"""
Base model using TFRecord pipeline
"""

from datetime import datetime
import os
import time
import sys
import tensorflow as tf
from tensorflow.contrib.tensorboard.plugins import projector
import numpy as np
import random
import pdb
from six import iteritems
import glob

sys.path.append('../')
from configs.train_config import TrainConfig
from data_io import event_generator, load_data_and_label, prepare_dataset
import networks
import utils

def select_triplets_facenet(lab, eve_embedding, triplet_per_batch, alpha=0.2, num_negative=3, metric="squaredeuclidean"):
    """
    Select the triplets for training
    1. Sample anchor-positive pair (try to balance imbalanced classes)
    2. Semi-hard negative mining used in facenet

    Arguments:
    lab -- array of labels, [N,]
    eve_embedding -- array of event embeddings, [N, emb_dim]
    triplet_per_batch -- int
    alpha -- float, margin
    num_negative -- number of negative samples per anchor-positive pairs
    metric -- metric to calculate distance
    """

    # get distance for all pairs
    all_diff = utils.all_diffs(eve_embedding, eve_embedding)
    all_dist = utils.cdist(all_diff, metric=metric)

    idx_dict = {}
    for i, l in enumerate(lab):
        l = int(l)
        if l not in idx_dict:
            idx_dict[l] = [i]
        else:
            idx_dict[l].append(i)
    for key in idx_dict:
        random.shuffle(idx_dict[key])

    # create iterators for each anchor-positive pair
    foreground_keys = [key for key in idx_dict.keys() if not key == 0]
    foreground_dict = {}
    for key in foreground_keys:
        foreground_dict[key] = itertools.permutations(idx_dict[key], 2)

    triplet_input_idx = []
    all_neg_count = []    # for monitoring active count
    while (len(triplet_input_idx)) < triplet_per_batch * 3:
        keys = list(foreground_dict.keys())
        if len(keys) == 0:
            break

        for key in keys:
            try:
                an_idx, pos_idx = foreground_dict[key].__next__()
            except:
                # remove the key to prevent infinite loop
                del foreground_dict[key]
                continue
            
            pos_dist = all_dist[an_idx, pos_idx]
            neg_dist = np.copy(all_dist[an_idx])    # important to make a copy, otherwise is reference
            neg_dist[idx_dict[key]] = np.NaN

            all_neg = np.where(np.logical_and(neg_dist-pos_dist < alpha,
                                            pos_dist < neg_dist))[0]
            all_neg_count.append(len(all_neg))

            # continue if no proper negtive sample 
            if len(all_neg) > 0:
                for i in range(num_negative):
                    neg_idx = all_neg[np.random.randint(len(all_neg))]

                    triplet_input_idx.extend([an_idx, pos_idx, neg_idx])
                    #triplet_input.append(np.expand_dims(eve[an_idx],0))
                    #triplet_input.append(np.expand_dims(eve[pos_idx],0))
                    #triplet_input.append(np.expand_dims(eve[neg_idx],0))

    if len(triplet_input) > 0:
        return triplet_input_idx, np.mean(all_neg_count)
#        return np.concatenate(triplet_input, axis=0), np.mean(all_neg_count)
    else:
        return None, None


"""
Reference:
    FaceNet implementation:
    https://github.com/davidsandberg/facenet
"""

def main():

    cfg = TrainConfig().parse()
    print (cfg.name)
    result_dir = os.path.join(cfg.result_root, 
            cfg.name+'_'+datetime.strftime(datetime.now(), '%Y%m%d-%H%M%S'))
    if not os.path.isdir(result_dir):
        os.makedirs(result_dir)
    utils.write_configure_to_file(cfg, result_dir)
    np.random.seed(seed=cfg.seed)

    # prepare dataset
    train_session = cfg.train_session
    tfrecords_files = glob.glob(cfg.tfrecords_root+'*.tfrecords')
    tfrecords_files = sorted(tfrecords_files)
    train_set = [f for f in tfrecords_files if os.path.basename(f).split('_')[0] in train_session]
    print ("Number of training events: %d" % len(train_set))

    val_session = cfg.val_session
    val_set = prepare_dataset(cfg.feature_root, val_session, cfg.feat, cfg.label_root)


    # construct the graph
    with tf.Graph().as_default():
        tf.set_random_seed(cfg.seed)
        global_step = tf.Variable(0, trainable=False)
        lr_ph = tf.placeholder(tf.float32, name='learning_rate')

        # load backbone model and get the embdding
        if cfg.network == "tsn":
            model = networks.ConvTSN(n_seg=cfg.num_seg, emb_dim=cfg.emb_dim)
            input_ph = tf.placeholder(tf.float32, shape=[None, cfg.num_seg, None, None, None])
            seqlen_ph = tf.placeholder(tf.int32, shape=[None])    # fake, for consistency
            model.forward(input_ph)

        elif cfg.network == "lstm":
            model = networks.ConvLSTM(max_time=cfg.MAX_LENGTH_FRAMES, emb_dim=cfg.emb_dim)
            input_ph = tf.placeholder(tf.float32, shape=[None, cfg.MAX_LENGTH_FRAMES, None, None, None])
            seqlen_ph = tf.placeholder(tf.int32, shape=[None])
            model.forward(input_ph, seqlen_ph)

        if cfg.normalized:
            embedding = tf.nn.l2_normalize(model.hidden, axis=-1, epsilon=1e-10)
        else:
            embedding = model.hidden

        # variable for visualizing the embeddings
        emb_var = tf.Variable([0.0], name='embeddings')
        set_emb = tf.assign(emb_var, embedding, validate_shape=False)

        # calculated for monitoring all-pair embedding distance
        diffs = utils.all_diffs_tf(embedding, embedding)
        all_dist = utils.cdist_tf(diffs)
        tf.summary.histogram('embedding_dists', all_dist)

        # split embedding into anchor, positive and negative and calculate triplet loss
        anchor, positive, negative = tf.unstack(tf.reshape(embedding, [-1,3,cfg.emb_dim]), 3, 1)
        triplet_loss = networks.triplet_loss(anchor, positive, negative, cfg.alpha)

        regularization_loss = tf.reduce_sum(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))
        total_loss = triplet_loss + regularization_loss * cfg.lambda_l2

        tf.summary.scalar('learning_rate', lr_ph)
        train_op = utils.optimize(total_loss, global_step, cfg.optimizer,
                lr_ph, tf.global_variables())

        saver = tf.train.Saver(max_to_keep=10)

        summary_op = tf.summary.merge_all()

        # session iterator for session sampling
        tf_paths_ph = tf.placeholder(tf.string, shape=[None])
        feat_dict = {'resnet': 98304}
        context_dict = {'label': 'int', 'length':'int'}
        train_data = event_generator(tf_paths_ph, feat_dict, context_dict,
                event_per_batch=cfg.event_per_batch, num_threads=4, shuffled=True,
                preprocess_func=model.prepare_input_tf)
        train_sess_iterator = train_data.make_initializable_iterator()
        next_train = train_sess_iterator.get_next()

        # prepare validation data
        val_feats = []
        val_labels = []
        val_lengths = []
        for session in val_set:
            eve_batch, lab_batch, bou_batch = load_data_and_label(session[0], session[1], model.prepare_input)
            val_feats.append(eve_batch)
            val_labels.append(lab_batch)
            val_lengths.extend([b[1]-b[0] for b in bou_batch])
        val_feats = np.concatenate(val_feats, axis=0)
        val_labels = np.concatenate(val_labels, axis=0)
        val_lengths = np.asarray(val_lengths, dtype='int32')
        print ("Shape of val_feats: ", val_feats.shape)

        # generate metadata.tsv for visualize embedding
        with open(os.path.join(result_dir, 'metadata_val.tsv'), 'w') as fout:
            for v in val_labels:
                fout.write('%d\n' % int(v))


        # Start running the graph
        if cfg.gpu:
            os.environ['CUDA_VISIBLE_DEVICES'] = cfg.gpu

        gpu_options = tf.GPUOptions(allow_growth=True)
        sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))

        summary_writer = tf.summary.FileWriter(result_dir, sess.graph)

        with sess.as_default():

            sess.run(tf.global_variables_initializer())

            # load pretrain model, if needed
            if cfg.pretrained_model:
                print ("Restoring pretrained model: %s" % cfg.pretrained_model)
                saver.restore(sess, cfg.pretrained_model)

            ################## Training loop ##################
            epoch = 0
            while epoch < cfg.max_epochs:
                step = sess.run(global_step, feed_dict=None)

                # learning rate schedule, reference: "In defense of Triplet Loss"
                if epoch < cfg.static_epochs:
                    learning_rate = cfg.learning_rate
                else:
                    learning_rate = cfg.learning_rate * \
                            0.001**((epoch-cfg.static_epochs)/(cfg.max_epochs-cfg.static_epochs))

                sess.run(train_sess_iterator.initializer, feed_dict={tf_paths_ph: train_set})

                # for each epoch
                batch_count = 1
                while True:
                    try:
                        start_time_select = time.time()

                        context, feature_lists = sess.run(next_train)
                        select_time = time.time() - start_time_select

                        eve = feature_lists[cfg.feat].reshape((-1, cfg.num_seg)+cfg.feat_dim[cfg.feat])
                        lab = context['label']
                        seq_len = context['length']

                        # Get the embeddings of all events
                        eve_embedding = np.zeros((eve.shape[0], cfg.emb_dim), dtype='float32')
                        for start, end in zip(range(0, eve.shape[0], cfg.batch_size),
                                            range(cfg.batch_size, eve.shape[0]+cfg.batch_size, cfg.batch_size)):
                            end = min(end, eve.shape[0])
                            emb = sess.run(embedding, feed_dict={input_ph: eve[start:end],
                                                                 seqlen_ph: seq_len[start:end]})
                            eve_embedding[start:end] = emb

                        # Second, sample triplets within sampled sessions
                        # return the triplet input indices
                        if cfg.triplet_select == 'random':
                            triplet_input = select_triplets_random(eve,lab,cfg.triplet_per_batch)
                            negative_count = 0
                        elif cfg.triplet_select == 'facenet':
                            if epoch < cfg.negative_epochs:
                                triplet_input = select_triplets_random(eve,lab,cfg.triplet_per_batch)
                                negative_count = 0
                            else:
                                triplet_input_idx, negative_count = select_triplets_facenet(lab,eve_embedding,cfg.triplet_per_batch,cfg.alpha,metric=cfg.metric)
                        else:
                            raise NotImplementedError

                        select_time2 = time.time()-start_time_select-select_time1


                        if triplet_input_idx is not None:

                            triplet_input = eve[triplet_input_idx]
                            triplet_length = seq_len[triplet_input_idx]

                            start_time_train = time.time()
                            # perform training on the selected triplets
                            err, _, step, summ = sess.run([total_loss, train_op, global_step, summary_op],
                                    feed_dict = {input_ph: triplet_input,
                                                seqlen_ph: triplet_length,
                                                lr_ph: learning_rate})

                            train_time = time.time() - start_time_train
                            print ("Epoch: [%d][%d/%d]\tEvent num: %d\tTriplet num: %d\tSelect_time1: %.3f\tSelect_time2: %.3f\tTrain_time: %.3f\tLoss %.4f" % \
                                    (epoch+1, batch_count, batch_per_epoch, eve.shape[0], triplet_input.shape[0], select_time1, select_time2, train_time, err))

                            summary = tf.Summary(value=[tf.Summary.Value(tag="train_loss", simple_value=err),
                                tf.Summary.Value(tag="negative_count", simple_value=negative_count),
                                tf.Summary.Value(tag="select_time1", simple_value=select_time1)])
                            summary_writer.add_summary(summary, step)
                            summary_writer.add_summary(summ, step)

                        batch_count += 1
                    
                    except tf.errors.OutOfRangeError:
                        print ("Epoch %d done!" % (epoch+1))
                        break

                # validation on val_set
                print ("Evaluating on validation set...")
                val_embeddings, _ = sess.run([embedding, set_emb], feed_dict={input_ph: val_feats, seqlen_ph: val_lengths})
                mAP, _ = utils.evaluate(val_embeddings, val_labels)

                summary = tf.Summary(value=[tf.Summary.Value(tag="Valiation mAP", simple_value=mAP)])
                summary_writer.add_summary(summary, step)

                # config for embedding visualization
                config = projector.ProjectorConfig()
                visual_embedding = config.embeddings.add()
                visual_embedding.tensor_name = emb_var.name
                visual_embedding.metadata_path = os.path.join(result_dir, 'metadata_val.tsv')
                projector.visualize_embeddings(summary_writer, config)

                # save model
                saver.save(sess, os.path.join(result_dir, cfg.name+'.ckpt'), global_step=step)

if __name__ == "__main__":
    main()
