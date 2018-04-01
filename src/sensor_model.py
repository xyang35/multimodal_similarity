"""
Similarity learning for CANBus sensor input
"""

from datetime import datetime
import os
import time
import sys
import tensorflow as tf
from tensorflow.contrib.tensorboard.plugins import projector
import numpy as np
import itertools
import random
import pdb
from six import iteritems
import glob

sys.path.append('../')
from configs.train_config import TrainConfig
from data_io import session_generator, load_data_and_label
import networks
import utils

def prepare_dataset(data_dir, sessions, feat, label_dir=None):

    if feat == 'resnet':
        appendix = '.npy'
    else:
        raise NotImplementedError

    dataset = []
    for sess in sessions:
        feat_path = os.path.join(data_dir, sess+appendix)
        label_path = os.path.join(label_dir, sess+'_goal.pkl')

        dataset.append((feat_path, label_path))

    return dataset

def write_configure_to_file(cfg, result_dir):
    with open(os.path.join(result_dir, 'config.txt'), 'w') as fout:
        for key, value in iteritems(vars(cfg)):
            fout.write('%s: %s\n' % (key, str(value)))

def select_triplets_random(eve, lab, triplet_per_batch, num_negative=3):
    """
    Select the triplets for training
    1. Sample anchor-positive pair (try to balance imbalanced classes)
    2. Randomly selecting negative sample for each anchor-positive pair

    Arguments:
    eve -- array of event features, [N, n_seg, (dims)]
    lab -- array of labels, [N,]
    triplet_per_batch -- int
    num_negative -- number of negative samples per anchor-positive pairs
    """

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

    triplet_input = []
    while (len(triplet_input)) < triplet_per_batch * 3:
        keys = list(foreground_dict.keys())
        if len(keys) == 0:
            break

        for key in keys:
            all_neg = np.where(lab!=key)[0]
            try:
                an_idx, pos_idx = foreground_dict[key].__next__()
            except:
                # remove the key to prevent infinite loop
                del foreground_dict[key]
                continue
            
            # randomly sample negative for the anchor-positive pair
            for i in range(num_negative):
                neg_idx = all_neg[np.random.randint(len(all_neg))]

                triplet_input.append(np.expand_dims(eve[an_idx],0))
                triplet_input.append(np.expand_dims(eve[pos_idx],0))
                triplet_input.append(np.expand_dims(eve[neg_idx],0))

    return np.concatenate(triplet_input, axis=0)

def select_triplets_facenet(eve, lab, eve_embedding, triplet_per_batch, alpha=0.2, num_negative=3):
    """
    Select the triplets for training
    1. Sample anchor-positive pair (try to balance imbalanced classes)
    2. Semi-hard negative mining used in facenet

    Arguments:
    eve -- array of event features, [N, n_seg, (dims)]
    lab -- array of labels, [N,]
    eve_embedding -- array of event embeddings, [N, emb_dim]
    triplet_per_batch -- int
    alpha -- float, margin
    num_negative -- number of negative samples per anchor-positive pairs
    """

    #FIXME: too slow, seems to take similar time for 100 / 300 triplets_per_batch

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

    triplet_input = []
    all_neg_count = []    # for monitoring active count
    while (len(triplet_input)) < triplet_per_batch * 3:
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
            
            pdb.set_trace()
            pos_dist_sqr = np.sum(np.square(eve_embedding[an_idx] - eve_embedding[pos_idx]))
            neg_dist_sqr = np.sum(np.square(eve_embedding[an_idx] - eve_embedding), 1)
            neg_dist_sqr[idx_dict[key]] = np.NaN

            all_neg = np.where(np.logical_and(neg_dist_sqr-pos_dist_sqr < alpha,
                                            pos_dist_sqr < neg_dist_sqr))[0]
            all_neg_count.append(len(all_neg))

            # continue if no proper negtive sample 
            if len(all_neg) > 0:
                for i in range(num_negative):
                    neg_idx = all_neg[np.random.randint(len(all_neg))]

                    triplet_input.append(np.expand_dims(eve[an_idx],0))
                    triplet_input.append(np.expand_dims(eve[pos_idx],0))
                    triplet_input.append(np.expand_dims(eve[neg_idx],0))

    if len(triplet_input) > 0:
        return np.concatenate(triplet_input, axis=0), np.mean(all_neg_count)
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
    write_configure_to_file(cfg, result_dir)
    np.random.seed(seed=cfg.seed)

    # prepare dataset
    train_session = cfg.train_session
    train_set = prepare_dataset(cfg.feature_root, train_session, cfg.feat, cfg.label_root)
    batch_per_epoch = len(train_set)//cfg.sess_per_batch

    val_session = cfg.val_session
    val_set = prepare_dataset(cfg.feature_root, val_session, cfg.feat, cfg.label_root)



    # construct the graph
    with tf.Graph().as_default():
        tf.set_random_seed(cfg.seed)
        global_step = tf.Variable(0, trainable=False)
        lr_ph = tf.placeholder(tf.float32, name='learning_rate')

        # load backbone model
        if cfg.network == "tsn":
            model = networks.ConvTSN(n_seg=cfg.num_seg, emb_dim=cfg.emb_dim)

        # get the embedding
        input_ph = tf.placeholder(tf.float32, shape=[None, cfg.num_seg, None, None, None])
        model.forward(input_ph)
        embedding = tf.nn.l2_normalize(model.hidden, axis=1, epsilon=1e-10, name='embedding')

        # variable for visualizing the embeddings
        emb_var = tf.Variable([0.0], name='embeddings')
        set_emb = tf.assign(emb_var, embedding, validate_shape=False)

        # calculated for monitoring all-pair embedding distance
        diffs = utils.all_diffs_tf(embedding, embedding)
        all_dist = tf.reduce_sum(tf.square(diffs), axis=-1)
        tf.summary.histogram('embedding_dists', all_dist)

        # split embedding into anchor, positive and negative and calculate triplet loss
        anchor, positive, negative = tf.unstack(tf.reshape(embedding, [-1,3,cfg.emb_dim]), 3, 1)
        triplet_loss = networks.triplet_loss(anchor, positive, negative, cfg.alpha)

        regularization_loss = tf.reduce_sum(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))
        total_loss = triplet_loss + regularization_loss * cfg.lambda_l2

#        learning_rate = tf.train.exponential_decay(lr_ph, global_step,
#                cfg.decay_epochs*(len(train_set)//cfg.sess_per_batch), 
#                cfg.decay_factor, staircase=True)

        tf.summary.scalar('learning_rate', lr_ph)
        train_op = utils.optimize(total_loss, global_step, cfg.optimizer,
                lr_ph, tf.global_variables())

        saver = tf.train.Saver(max_to_keep=10)

        summary_op = tf.summary.merge_all()

        # session iterator for session sampling
        feat_paths_ph = tf.placeholder(tf.string, shape=[None, cfg.sess_per_batch])
        label_paths_ph = tf.placeholder(tf.string, shape=[None, cfg.sess_per_batch])
        train_data = session_generator(feat_paths_ph, label_paths_ph, sess_per_batch=cfg.sess_per_batch, num_threads=2, shuffled=False, preprocess_func=model.prepare_input)
        train_sess_iterator = train_data.make_initializable_iterator()
        next_train = train_sess_iterator.get_next()

        # prepare validation data
        val_feats = []
        val_labels = []
        for session in val_set:
            eve_batch, lab_batch = load_data_and_label(session[0], session[1], model.prepare_input)
            val_feats.append(eve_batch)
            val_labels.append(lab_batch)
        val_feats = np.concatenate(val_feats, axis=0)
        val_labels = np.concatenate(val_labels, axis=0)
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
            epoch = -1
            while epoch < cfg.max_epochs-1:
                step = sess.run(global_step, feed_dict=None)
                epoch = step // batch_per_epoch

                # learning rate schedule, reference: "In defense of Triplet Loss"
                if epoch < cfg.static_epochs:
                    learning_rate = cfg.learning_rate
                else:
                    learning_rate = cfg.learning_rate * \
                            0.001**((epoch-cfg.static_epochs)/(cfg.max_epochs-cfg.static_epochs))

                # prepare data for this epoch
                random.shuffle(train_set)

                feat_paths = [path[0] for path in train_set]
                label_paths = [path[1] for path in train_set]
                # reshape a list to list of list
                # interesting hacky code from: https://stackoverflow.com/questions/10124751/convert-a-flat-list-to-list-of-list-in-python
                feat_paths = list(zip(*[iter(feat_paths)]*cfg.sess_per_batch))
                label_paths = list(zip(*[iter(label_paths)]*cfg.sess_per_batch))

                sess.run(train_sess_iterator.initializer, feed_dict={feat_paths_ph: feat_paths,
                  label_paths_ph: label_paths})

                # for each epoch
                batch_count = 1
                while True:
                    try:
                        # Hierarchical sampling (same as fast rcnn)
                        start_time_select = time.time()

                        # First, sample sessions for a batch
                        eve, se, lab = sess.run(next_train)

                        select_time1 = time.time() - start_time_select

                        # Get the embeddings of all events
                        eve_embedding = np.zeros((eve.shape[0], cfg.emb_dim), dtype='float32')
                        for start, end in zip(range(0, eve.shape[0], cfg.batch_size),
                                            range(cfg.batch_size, eve.shape[0]+cfg.batch_size, cfg.batch_size)):
                            end = min(end, eve.shape[0])
                            emb = sess.run(embedding, feed_dict={input_ph: eve[start:end]})
                            eve_embedding[start:end] = emb

                        # Second, sample triplets within sampled sessions
                        if cfg.triplet_select == 'random':
                            triplet_input = select_triplets_random(eve,lab,cfg.triplet_per_batch)
                        elif cfg.triplet_select == 'facenet':

                            triplet_input, negative_count = select_triplets_facenet(eve,lab,eve_embedding,cfg.triplet_per_batch,cfg.alpha)
                        else:
                            raise NotImplementedError

                        select_time2 = time.time()-start_time_select-select_time1


                        if triplet_input is not None:
                            start_time_train = time.time()
                            # perform training on the selected triplets
                            err, _, step, summ = sess.run([total_loss, train_op, global_step, summary_op],
                                    feed_dict = {input_ph: triplet_input,
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
                val_embeddings, _ = sess.run([embedding, set_emb], feed_dict={input_ph: val_feats})
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