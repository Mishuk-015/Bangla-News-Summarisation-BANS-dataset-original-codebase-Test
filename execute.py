# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time

import numpy as np
# from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq2seq_model

from configparser import ConfigParser # In Python 3, ConfigParser has been renamed to configparser for PEP 8 compliance.
    
gConfig = {}

def get_config(config_file='seq2seq.ini'):
    parser = ConfigParser()
    parser.read(config_file)
    # get the ints, floats and strings
    _conf_ints = [ (key, int(value)) for key,value in parser.items('ints') ]
    _conf_floats = [ (key, float(value)) for key,value in parser.items('floats') ]
    _conf_strings = [ (key, str(value)) for key,value in parser.items('strings') ]
    return dict(_conf_ints + _conf_floats + _conf_strings)

# We use a number of buckets and pad to the closest one for efficiency.
# Change bucket sizes and numbers if you use your own dataset. 
_buckets = [(30, 10), (30, 20), (40, 10), (40, 20), (50, 20)]  

def read_data(source_path, target_path, max_size=None):
  """Read data from source and target files and put into buckets.

  Args:
    source_path: path to the files with token-ids for the source language.
    target_path: path to the file with token-ids for the target language;
      it must be aligned with the source file: n-th line contains the desired
      output for n-th line from the source_path.
    max_size: maximum number of lines to read, all other will be ignored;
      if 0 or None, data files will be read completely (no limit).

  Returns:
    data_set: a list of length len(_buckets); data_set[n] contains a list of
      (source, target) pairs read from the provided data files that fit
      into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
      len(target) < _buckets[n][1]; source and target are lists of token-ids.
  """
  data_set = [[] for _ in _buckets]
  with tf.gfile.GFile(source_path, mode="r") as source_file:
    with tf.gfile.GFile(target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target and (not max_size or counter < max_size):
        counter += 1
        if counter % 1000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = [int(x) for x in source.split()]
        target_ids = [int(x) for x in target.split()]
        target_ids.append(data_utils.EOS_ID)
        for bucket_id, (source_size, target_size) in enumerate(_buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids, target_ids])
            break
        source, target = source_file.readline(), target_file.readline()
  return data_set


def create_model(session, forward_only):

  """Create model and initialize or load parameters"""
  model = seq2seq_model.Seq2SeqModel( gConfig['enc_vocab_size'], gConfig['dec_vocab_size'], _buckets, gConfig['hidden_units'], gConfig['num_layers'], gConfig['max_gradient_norm'], gConfig['batch_size'], gConfig['learning_rate'], gConfig['learning_rate_decay_factor'], forward_only=forward_only)

  if 'pretrained_model' in gConfig:
      model.saver.restore(session,gConfig['pretrained_model'])
      return model

  ckpt = tf.train.get_checkpoint_state(gConfig['working_directory'])
  if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
    print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    print("Created model with fresh parameters.")
    session.run(tf.initialize_all_variables())
  return model


def train():
  # prepare dataset
  print("Preparing data in %s" % gConfig['working_directory'])
  enc_train, dec_train, enc_dev, dec_dev, _, _ = data_utils.prepare_custom_data(gConfig['working_directory'], gConfig['train_enc'],gConfig['train_dec'],gConfig['eval_enc'],gConfig['eval_dec'],gConfig['enc_vocab_size'],gConfig['dec_vocab_size'])

  # setup config to use BFC allocator
  config = tf.ConfigProto()  
  config.gpu_options.allocator_type = 'BFC'

  with tf.Session(config=config) as sess:
    # Create model.
    print("Creating %d layers of %d units." % (gConfig['num_layers'], gConfig['hidden_units']))
    model = create_model(sess, False)

    # Read data into buckets and compute their sizes.
    print ("Reading development and training data (limit: %d)."
           % gConfig['max_train_data_size'])
    dev_set = read_data(enc_dev, dec_dev)
    train_set = read_data(enc_train, dec_train, gConfig['max_train_data_size'])
    train_bucket_sizes = [len(train_set[b]) for b in range(len(_buckets))]
    train_total_size = float(sum(train_bucket_sizes))

    # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
    # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
    # the size if i-th training bucket, as used later.
    train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                           for i in range(len(train_bucket_sizes))]

    # This is the training loop.
    step_time, loss = 0.0, 0.0
    current_step = 0
    previous_losses = []
    while True:
      # Choose a bucket according to data distribution. We pick a random number
      # in [0, 1] and use the corresponding interval in train_buckets_scale.
      random_number_01 = np.random.random_sample()
      bucket_id = min([i for i in range(len(train_buckets_scale))
                       if train_buckets_scale[i] > random_number_01])

      # Get a batch and make a step.
      start_time = time.time()
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          train_set, bucket_id)
      _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                   target_weights, bucket_id, False)
      step_time += (time.time() - start_time) / gConfig['steps_per_checkpoint']
      loss += step_loss / gConfig['steps_per_checkpoint']
      current_step += 1

      # Once in a while, we save checkpoint, print statistics, and run evals.
      if current_step % gConfig['steps_per_checkpoint'] == 0:
        # Print statistics for the previous epoch.
        perplexity = math.exp(loss) if loss < 300 else float('inf')
        print ("global step %d learning rate %.4f step-time %.2f perplexity "
               "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                         step_time, perplexity))
        # Decrease learning rate if no improvement was seen over last 3 times.
        if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
          sess.run(model.learning_rate_decay_op)
        previous_losses.append(loss)
        # Save checkpoint and zero timer and loss.
        checkpoint_path = os.path.join(gConfig['working_directory'], "seq2seq.ckpt")
        model.saver.save(sess, checkpoint_path, global_step=model.global_step)
        step_time, loss = 0.0, 0.0
        # Run evals on development set and print their perplexity.
        for bucket_id in range(len(_buckets)):
          if len(dev_set[bucket_id]) == 0:
            print("  eval: empty bucket %d" % (bucket_id))
            continue
          encoder_inputs, decoder_inputs, target_weights = model.get_batch(
              dev_set, bucket_id)
          _, eval_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                       target_weights, bucket_id, True)
          eval_ppx = math.exp(eval_loss) if eval_loss < 300 else float('inf')
          print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))
        sys.stdout.flush()


def decode():
  with tf.Session() as sess:
    # Create model and load parameters.
    model = create_model(sess, True)
    model.batch_size = 1  # We decode one sentence at a time.

    # Load vocabularies.
    enc_vocab_path = os.path.join(gConfig['working_directory'],"vocab%d_enc.txt" % gConfig['enc_vocab_size'])
    dec_vocab_path = os.path.join(gConfig['working_directory'],"vocab%d_dec.txt" % gConfig['dec_vocab_size'])

    enc_vocab, _ = data_utils.initialize_vocabulary(enc_vocab_path)
    _, rev_dec_vocab = data_utils.initialize_vocabulary(dec_vocab_path)



    # Decode sentence and store it
    with open(gConfig["test_enc"], 'r') as test_enc:
        with open(gConfig["output"], 'w') as predicted_summary:
            sentence_count = 0
            for sentence in test_enc:
                # Get token-ids for the input sentence.
                token_ids = data_utils.sentence_to_token_ids(sentence, enc_vocab)
                # Which bucket does it belong to? And place the sentence to the last bucket if its token length is larger then X.
                bucket_id = min([b for b in range(len(_buckets)) if _buckets[b][0] > len(token_ids)] + [len(_buckets)-1])
                # Get a 1-element batch to feed the sentence to the model.
                encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                {bucket_id: [(token_ids, [])]}, bucket_id)
                # Get output logits for the sentence.
                _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                           target_weights, bucket_id, True)

                # This is a greedy decoder - outputs are just argmaxes of output_logits.
                outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]

                # If there is an EOS symbol in outputs, cut them at that point.
                if data_utils.EOS_ID in outputs:
                    outputs = outputs[:outputs.index(data_utils.EOS_ID)]
                # Write predicted summary corresponding to article.
                # print(len(rev_dec_vocab))
                predicted_summary.write(" ".join([tf.compat.as_str(rev_dec_vocab[output]) for output in outputs])+'\n')
                sentence_count += 1
                if sentence_count % 100 == 0:
                    print("predicted data line %d" % sentence_count)
                    sys.stdout.flush()

        predicted_summary.close()
    test_enc.close()

    print("Finished decoding and stored predicted results in %s!" % gConfig["output"])

def decode_input():
    interactive_predicted_summary=open("/home/avivilla/Desktop/deep/output/testing/interactive_summarys.txt","a+")
    with tf.Session() as sess:
      # Create model and load parameters.
      model = create_model(sess, True)
      model.batch_size = 1  # We decode one sentence at a time.

      # Load vocabularies.
      enc_vocab_path = os.path.join(gConfig['working_directory'],"vocab%d_enc.txt" % gConfig['enc_vocab_size'])
      dec_vocab_path = os.path.join(gConfig['working_directory'],"vocab%d_dec.txt" % gConfig['dec_vocab_size'])

      enc_vocab, _ = data_utils.initialize_vocabulary(enc_vocab_path)
      _, rev_dec_vocab = data_utils.initialize_vocabulary(dec_vocab_path)


      # Decode from standard input.
      sys.stdout.write("> ")
      sys.stdout.flush()
      sentence = sys.stdin.readline()
    
      while sentence:
        # Get token-ids for the input sentence.
        token_ids = data_utils.sentence_to_token_ids(sentence, enc_vocab)
        # Which bucket does it belong to? And place the sentence to the last bucket if its token length is larger then the bucket length.
        bucket_id = min([b for b in range(len(_buckets)) if _buckets[b][0] > len(token_ids)] + [len(_buckets)-1])
        # Get a 1-element batch to feed the sentence to the model.
        encoder_inputs, decoder_inputs, target_weights = model.get_batch(
            {bucket_id: [(token_ids, [])]}, bucket_id)
        # Get output logits for the sentence.
        _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                         target_weights, bucket_id, True)
        # This is a greedy decoder - outputs are just argmaxes of output_logits.
        outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]


        # If there is an EOS symbol in outputs, cut them at that point.
        if data_utils.EOS_ID in outputs:
          outputs = outputs[:outputs.index(data_utils.EOS_ID)]
        # Print out French sentence corresponding to outputs.
        interactive_predicted_summary.write(" ".join([tf.compat.as_str(rev_dec_vocab[output]) for output in outputs])+'\n')

        print("> ", end="")
        sys.stdout.flush()
        sentence = sys.stdin.readline()
    interactive_predicted_summary.close()
    
if __name__ == '__main__':
    # get configuration from seq2seq.ini
    gConfig = get_config()

    print('\n>> Mode : %s\n' %(gConfig['mode']))

    if gConfig['mode'] == 'train':
        # start training
        train()
    elif gConfig['mode'] == 'test':
        # start testing
        decode()
    elif gConfig['mode'] == 'interactive':
        # start interactive decoding
        decode_input()

