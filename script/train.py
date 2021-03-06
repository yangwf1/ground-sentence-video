"""
train.py: Train the Temporally Grounding Network (TGN) model

Usage:
    train.py (tacos | acnet) --textual-data-path=<dir> --visual-data-path=<dir> [options]

Options:
    -h --help                               show this screen
    --textual-data-path=<dir>               directory containing the annotations
    --visual-data-path=<dir>                directory containing the videos
    --K=<int>                               parameter K in the paper
    --delta=<int>                           parameter ẟ in the paper
    --threshold=<float>                     parameter θ in the paper
    --batch-size=<int>                      batch size [default: 64]
    --hidden-size-textual-lstm=<int>        hidden size of textual lstm [default: 512]
    --hidden-size-visual-lstm=<int>         hidden size of visual lstm [default: 512]
    --hidden-size-ilstm=<int>               hidden size of ilstm [default: 512]
    --log-every=<int>                       log every [default: 10]
    --max-iter=<int>                        maximum number of iterations of training [default: 10000]
    --lr=<float>                            learning rate [default: 0.001]
    --patience=<int>                        waiting for how many iterations to decay learning rate [default: 2]
    --max-num-trial=<int>                   terminate training after how many trials [default: 3]
    --model-save-path=<file>                model save path [default: model.bin]
    --valid-niter=<int>                     perform validation after how many iterations [default: 50]
    --top-n-eval=<int>                      parameter N in R@N, IOU=θ evaluation metric [default: 1]
    --lr-decay=<float>                      learning rate decay [default: 0.5]
"""

import torch
import torch.nn as nn
from models.tgn import TGN
from docopt import docopt
from typing import Dict, List
from vocab import Vocab
from utils import load_word_vectors, find_bce_weights, compute_overlap, top_n_iou
import sys
from data import TACoS, ActivityNet
from tqdm import tqdm
from time import time
from torch.nn.init import xavier_normal_, normal_
from torch.utils.tensorboard import SummaryWriter
import numpy as np
from math import ceil


def validation(model: TGN, dataset, device, embedding: nn.Embedding, args: Dict):
    was_training = model.training

    batch_size = int(args['--batch-size'])

    with torch.no_grad():
        cum_score = cum_samples = 0

        pbar = tqdm(total=ceil(len(dataset.val_captions) / batch_size))

        for textual_data, visual_data in iter(dataset.data_iter(batch_size, 'val')):
            cum_samples += len(textual_data)
            lengths_t = [len(t) for t in textual_data]
            sents = [t.sent for t in textual_data]
            textual_data_tensor = vocab.to_input_tensor(sents, device=device)  # tensor with shape (n_batch, N)
            textual_data_embed_tensor = embedding(textual_data_tensor)  # tensor with shape (n_batch, N, embed_size)

            probs, mask = model(visual_data, textual_data_embed_tensor, lengths_t)  # Tensors with shape (n_batch, T, K)

            gold_start_times = [t.start_time for t in textual_data]
            gold_end_times = [t.end_time for t in textual_data]

            score = top_n_iou(probs*mask, gold_start_times, gold_end_times, args, dataset.fps, dataset.sample_rate)
            cum_score += score
            pbar.update()

        pbar.close()

    if was_training:
        model.train()

    return cum_score / cum_samples


def train(dataset, vocab: Vocab, word_vectors: np.ndarray, args: Dict, device):
    max_iter = int(args['--max-iter'])
    valid_niter = int(args['--valid-niter'])
    batch_size = int(args['--batch-size'])
    lr = float(args['--lr'])
    log_every = int(args['--log-every'])
    K = int(args['--K'])
    model_save_path = args['--model-save-path']

    embedding = nn.Embedding(len(vocab), word_vectors.shape[1], padding_idx=vocab.word2id['<pad>'])
    embedding.weight = nn.Parameter(data=torch.from_numpy(word_vectors).to(torch.float32), requires_grad=False)
    
    model = TGN(hidden_size_ilstm=int(args['--hidden-size-ilstm']),
                hidden_size_textual=int(args['--hidden-size-textual-lstm']),
                hidden_size_visual=int(args['--hidden-size-visual-lstm']),
                K=K, word_embed_size=word_vectors.shape[1], 
                visual_feature_size=dataset.visual_feature_size)

    model.train()

    for p in model.parameters():
        if p.requires_grad:
            if len(p.data.shape) > 1:
                xavier_normal_(p.data)
            else:
                normal_(p.data)


    model = model.to(device)
    embedding.to(device)
    optimizer = torch.optim.Adam(params=model.parameters(), lr=lr, betas=(0.5, 0.999))

    writer = SummaryWriter()

    w0, w1 = find_bce_weights(dataset, K, device)  # Tensors with shape (K,)

    cum_samples = report_samples = 0.
    report_loss = cum_loss = 0.

    val_scores = []
    patience = num_trial = 0

    train_time = begin_time = time()
    print('begin training...')

    iteration = 0
    while iteration <= max_iter:
        for textual_data, visual_features, y in dataset.data_iter(batch_size, 'train'):
            
            lengths_t = [len(t) for t in textual_data]
            textual_data_tensor = vocab.to_input_tensor(textual_data, device=device)  # tensor with shape (n_batch, N)
            textual_data_embed_tensor = embedding(textual_data_tensor)  # tensor with shape (n_batch, N, embed_size)
            
            optimizer.zero_grad()
            
            # Computing probs and mask with shape (n_batch, T, K)
            probs, mask = model(textual_input=textual_data_embed_tensor, features_v=visual_features, lengths_t=lengths_t)

            y = y.to(device)
                        
            batch_loss = -torch.sum((w0 * y * torch.log(probs) + w1 * (1 - y) * torch.log(1 - probs)) * mask)
            batch_loss_val = batch_loss.item()

            cum_samples += len(textual_data)
            report_samples += len(textual_data)
            report_loss += batch_loss_val
            cum_loss += batch_loss_val

            batch_loss.backward()
            optimizer.step()

            if iteration % log_every == 0:
                print('iteration number %d, loss: %f, '
                      'speed %.2f samples/sec, time elapsed %.2f sec' % (iteration,
                                                                         report_loss / report_samples,
                                                                         report_samples / (time() - train_time),
                                                                         time() - begin_time))

                writer.add_scalar('Loss/train', report_loss/report_samples, iteration)
                report_samples = 0
                report_loss = 0.
                train_time = time()

            if iteration % valid_niter == 0:
                print('\niteration number %d, cum. loss %.2f, cum. samples %d' % (iteration, 
                                                                                  cum_loss / cum_samples, 
                                                                                  cum_samples))

                cum_samples = 0
                cum_loss = 0.

                print('begin Validation...')
                val_score = validation(model=model, dataset=dataset, device=device, embedding=embedding, args=args)

                print('validation score %f' % val_score)
                writer.add_scalar('score/val', val_score, iteration)

                is_better = len(val_scores) == 0 or val_score > np.max(val_scores)
                val_scores.append(val_score)

                if is_better:
                    patience = 0
                    print('save currently the best model to [%s]' % model_save_path, file=sys.stderr)
                    model.save(model_save_path)
                    torch.save(optimizer.state_dict(), model_save_path + '.optim')
                elif patience < int(args['--patience']):
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)

                    if patience == int(args['--patience']):
                        num_trial += 1
                        print('hit trial %d' % num_trial, file=sys.stderr)
                        if num_trial == int(args['--max-num-trial']):
                            print('early stop!', file=sys.stderr)
                            exit(0)

                        lr = optimizer.param_groups[0]['lr'] * float(args['--lr-decay'])

                        print('load previously best model and decay learning rate to %f' % lr, file=sys.stderr)
                        params = torch.load(model_save_path, map_location=lambda storage, loc: storage)
                        model.load_state_dict(params['state_dict'])
                        model = model.to(device)

                        print('restore parameters of the optimizers', file=sys.stderr)
                        optimizer.load_state_dict(torch.load(model_save_path + '.optim'))

                        # set new learning rate
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr

                        patience = 0
            iteration += 1

    
    writer.close()


if __name__ == '__main__':
    args = docopt(__doc__)
    word_embed_size=50
    words, word_vectors = load_word_vectors('glove.6B.{}d.txt'.format(word_embed_size))

    vocab = Vocab(words)
    
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    print('use device: %s' % device, file=sys.stderr)
    
    if args['tacos']:
        dataset = TACoS(textual_data_path=args['--textual-data-path'], 
                        visual_data_path=args['--visual-data-path'], 
                        K = int(args['--K']), delta = int(args['--delta']), 
                        threshold = float(args['--threshold']))
    elif args['acnet']:
        dataset = ActivityNet(textual_data_path=args['--textual-data-path'], 
                              visual_data_path=args['--visual-data-path'], 
                              K = int(args['--K']), delta = int(args['--delta']), 
                              threshold = float(args['--threshold']))
        
    train(dataset, vocab, word_vectors, args, device)
        