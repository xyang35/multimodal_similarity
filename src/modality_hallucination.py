"""
Reference: Learning with Side Information through Modality Hallucination
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
from sklearn.metrics import accuracy_score

sys.path.append('../')
from configs.train_config import TrainConfig
from data_io import multimodal_session_generator, load_data_and_label, prepare_multimodal_dataset
import networks
import utils



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
    train_set = prepare_multimodal_dataset(cfg.feature_root, train_session, cfg.feat, cfg.label_root)
    train_set = train_set[:cfg.label_num]
    batch_per_epoch = len(train_set)//cfg.sess_per_batch

    val_session = cfg.val_session
    val_set = prepare_multimodal_dataset(cfg.feature_root, val_session, cfg.feat, cfg.label_root)


    # construct the graph
    with tf.Graph().as_default():
        tf.set_random_seed(cfg.seed)
        global_step = tf.Variable(0, trainable=False)
        lr_ph = tf.placeholder(tf.float32, name='learning_rate')

        
        ####################### Load models here ########################

        with tf.variable_scope("modality_core"):
            # load backbone model
            if cfg.network == "convtsn":
                model_emb = networks.ConvTSN(n_seg=cfg.num_seg, emb_dim=cfg.emb_dim)
            elif cfg.network == "convrtsn":
                model_emb = networks.ConvRTSN(n_seg=cfg.num_seg, emb_dim=cfg.emb_dim)
            else:
                raise NotImplementedError

            input_ph = tf.placeholder(tf.float32, shape=[None, cfg.num_seg, None, None, None])
            dropout_ph = tf.placeholder(tf.float32, shape=[])
            model_emb.forward(input_ph, dropout_ph)    # for lstm has variable scope

        with tf.variable_scope("modality_sensors"):
            sensors_emb_dim = 32
            model_emb_sensors = networks.RTSN(n_seg=cfg.num_seg, emb_dim=sensors_emb_dim)

            input_sensors_ph = tf.placeholder(tf.float32, shape=[None, cfg.num_seg, 8])
            model_emb_sensors.forward(input_sensors_ph, dropout_ph)

            var_list = {}
            for v in tf.global_variables():
                if v.op.name.startswith("modality_sensors"):
                    var_list[v.op.name.replace("modality_sensors/","")] = v
            restore_saver_sensors = tf.train.Saver(var_list)

        with tf.variable_scope("hallucination_sensors"):
            # load backbone model
            if cfg.network == "convtsn":
                hal_emb_sensors = networks.ConvTSN(n_seg=cfg.num_seg, emb_dim=sensors_emb_dim)
            elif cfg.network == "convrtsn":
                hal_emb_sensors = networks.ConvRTSN(n_seg=cfg.num_seg, emb_dim=sensors_emb_dim)
            else:
                raise NotImplementedError

            hal_emb_sensors.forward(input_ph, dropout_ph)    # for lstm has variable scope

        with tf.variable_scope("modality_segment"):
            segment_emb_dim = 32
            model_emb_segment = networks.RTSN(n_seg=cfg.num_seg, emb_dim=segment_emb_dim, n_input=357)

            input_segment_ph = tf.placeholder(tf.float32, shape=[None, cfg.num_seg, 357])
            model_emb_segment.forward(input_segment_ph, dropout_ph)

            var_list = {}
            for v in tf.global_variables():
                if v.op.name.startswith("modality_segment"):
                    var_list[v.op.name.replace("modality_segment/","")] = v
            restore_saver_segment = tf.train.Saver(var_list)

        with tf.variable_scope("hallucination_segment"):
            # load backbone model
            if cfg.network == "convtsn":
                hal_emb_segment = networks.ConvTSN(n_seg=cfg.num_seg, emb_dim=segment_emb_dim)
            elif cfg.network == "convrtsn":
                hal_emb_segment = networks.ConvRTSN(n_seg=cfg.num_seg, emb_dim=segment_emb_dim)
            else:
                raise NotImplementedError

            hal_emb_segment.forward(input_ph, dropout_ph)    # for lstm has variable scope

        ############################# Forward Pass #############################


        # Core branch
        if cfg.normalized:
            embedding = tf.nn.l2_normalize(model_emb.hidden, axis=-1, epsilon=1e-10)
            embedding_sensors = tf.nn.l2_normalize(model_emb_sensors.hidden, axis=-1, epsilon=1e-10)
            embedding_hal_sensors = tf.nn.l2_normalize(hal_emb_sensors.hidden, axis=-1, epsilon=1e-10)
            embedding_segment = tf.nn.l2_normalize(model_emb_segment.hidden, axis=-1, epsilon=1e-10)
            embedding_hal_segment = tf.nn.l2_normalize(hal_emb_segment.hidden, axis=-1, epsilon=1e-10)
        else:
            embedding = model_emb.hidden
            embedding_sensors = model_emb_sensors.hidden
            embedding_hal_sensors = hal_emb_sensors.hidden
            embedding_segment = model_emb_segment.hidden
            embedding_hal_segment = hal_emb_segment.hidden

        # variable for visualizing the embeddings
        emb_var = tf.Variable([0.0], name='embeddings')
        set_emb = tf.assign(emb_var, embedding, validate_shape=False)

        # calculated for monitoring all-pair embedding distance
        diffs = utils.all_diffs_tf(embedding, embedding)
        all_dist = utils.cdist_tf(diffs)
        tf.summary.histogram('embedding_dists', all_dist)

        # split embedding into anchor, positive and negative and calculate triplet loss
        anchor, positive, negative = tf.unstack(tf.reshape(embedding, [-1,3,cfg.emb_dim]), 3, 1)
        anc_sensors, pos_sensors, neg_sensors = tf.unstack(tf.reshape(embedding_sensors, [-1,3,sensors_emb_dim]), 3, 1)
        anc_hal_sensors, pos_hal_sensors, neg_hal_sensors = tf.unstack(tf.reshape(embedding_hal_sensors, [-1,3,sensors_emb_dim]), 3, 1)
        anc_segment, pos_segment, neg_segment = tf.unstack(tf.reshape(embedding_segment, [-1,3,segment_emb_dim]), 3, 1)
        anc_hal_segment, pos_hal_segment, neg_hal_segment = tf.unstack(tf.reshape(embedding_hal_segment, [-1,3,segment_emb_dim]), 3, 1)

        # a fusion embedding
        anc_fused = tf.concat((anchor, anc_hal_sensors, anc_hal_segment), axis=1)
        pos_fused = tf.concat((positive, pos_hal_sensors, anc_hal_segment), axis=1)
        neg_fused = tf.concat((negative, neg_hal_sensors, anc_hal_segment), axis=1)

        ############################# Calculate loss #############################

        # triplet loss
        metric_loss = networks.triplet_loss(anchor, positive, negative, cfg.alpha) + \
                      networks.triplet_loss(anc_sensors, pos_sensors, neg_sensors, cfg.alpha) + \
                      networks.triplet_loss(anc_hal_sensors, pos_hal_sensors, neg_hal_sensors, cfg.alpha) + \
                      networks.triplet_loss(anc_segment, pos_segment, neg_segment, cfg.alpha) + \
                      networks.triplet_loss(anc_hal_segment, pos_hal_segment, neg_hal_segment, cfg.alpha) + \
                      networks.triplet_loss(anc_fused, pos_fused, neg_fused, cfg.alpha)

        # hallucination loss (regression loss)
        hal_loss_sensors = tf.nn.l2_loss(embedding_sensors - embedding_hal_sensors)
        hal_loss_segment = tf.nn.l2_loss(embedding_segment - embedding_hal_segment)
        hal_loss = hal_loss_sensors + hal_loss_segment

        regularization_loss = tf.reduce_sum(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))
        # use lambda_multimodal for hal_loss
        total_loss = metric_loss + cfg.lambda_multimodal * hal_loss + regularization_loss * cfg.lambda_l2

        tf.summary.scalar('learning_rate', lr_ph)
        train_op = utils.optimize(total_loss, global_step, cfg.optimizer,
                                           lr_ph, tf.global_variables())

        saver = tf.train.Saver(max_to_keep=10)
        summary_op = tf.summary.merge_all()    # not logging histogram of variables because it will cause problem when only unimodal_train_op is called

        #########################################################################

        # session iterator for session sampling
        feat_paths_ph = tf.placeholder(tf.string, shape=[None, cfg.sess_per_batch])
        feat2_paths_ph = tf.placeholder(tf.string, shape=[None, cfg.sess_per_batch])
        feat3_paths_ph = tf.placeholder(tf.string, shape=[None, cfg.sess_per_batch])
        label_paths_ph = tf.placeholder(tf.string, shape=[None, cfg.sess_per_batch])
        train_data = multimodal_session_generator(feat_paths_ph, feat2_paths_ph, feat3_paths_ph, label_paths_ph, sess_per_batch=cfg.sess_per_batch, num_threads=2, shuffled=False, preprocess_func=[model_emb.prepare_input, model_emb_sensors.prepare_input, model_emb_segment.prepare_input])
        train_sess_iterator = train_data.make_initializable_iterator()
        next_train = train_sess_iterator.get_next()

        # prepare validation data
        val_sess = []
        val_feats = []
        val_feats2 = []
        val_feats3 = []
        val_labels = []
        val_boundaries = []
        for session in val_set:
            session_id = os.path.basename(session[1]).split('_')[0]
            eve_batch, lab_batch, boundary = load_data_and_label(session[0], session[-1], model_emb.prepare_input_test)    # use prepare_input_test for testing time
            val_feats.append(eve_batch)
            val_labels.append(lab_batch)
            val_sess.extend([session_id]*eve_batch.shape[0])
            val_boundaries.extend(boundary)

            eve2_batch, _,_ = load_data_and_label(session[1], session[-1], model_emb_sensors.prepare_input_test)
            val_feats2.append(eve2_batch)

            eve3_batch, _,_ = load_data_and_label(session[2], session[-1], model_emb_segment.prepare_input_test)
            val_feats3.append(eve3_batch)
        val_feats = np.concatenate(val_feats, axis=0)
        val_feats2 = np.concatenate(val_feats2, axis=0)
        val_feats3 = np.concatenate(val_feats3, axis=0)
        val_labels = np.concatenate(val_labels, axis=0)
        print ("Shape of val_feats: ", val_feats.shape)

        # generate metadata.tsv for visualize embedding
        with open(os.path.join(result_dir, 'metadata_val.tsv'), 'w') as fout:
            fout.write('id\tlabel\tsession_id\tstart\tend\n')
            for i in range(len(val_sess)):
                fout.write('{0}\t{1}\t{2}\t{3}\t{4}\n'.format(i, val_labels[i,0], val_sess[i],
                                            val_boundaries[i][0], val_boundaries[i][1]))

        #########################################################################


        # Start running the graph
        if cfg.gpu:
            os.environ['CUDA_VISIBLE_DEVICES'] = cfg.gpu

        gpu_options = tf.GPUOptions(allow_growth=True)
        sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))

        summary_writer = tf.summary.FileWriter(result_dir, sess.graph)

        with sess.as_default():

            sess.run(tf.global_variables_initializer())

            print ("Restoring sensors model: %s" % cfg.sensors_path)
            restore_saver_sensors.restore(sess, cfg.sensors_path)
            print ("Restoring segment model: %s" % cfg.segment_path)
            restore_saver_segment.restore(sess, cfg.segment_path)

            # load pretrain model, if needed
            if cfg.model_path:
                print ("Restoring pretrained model: %s" % cfg.model_path)
                saver.restore(sess, cfg.model_path)


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
                            0.01**((epoch-cfg.static_epochs)/(cfg.max_epochs-cfg.static_epochs))

                # prepare data for this epoch
                random.shuffle(train_set)

                paths = list(zip(*[iter(train_set)]*cfg.sess_per_batch))

                feat_paths = [[p[0] for p in path] for path in paths]
                feat2_paths = [[p[1] for p in path] for path in paths]
                feat3_paths = [[p[2] for p in path] for path in paths]
                label_paths = [[p[-1] for p in path] for path in paths]

                sess.run(train_sess_iterator.initializer, feed_dict={feat_paths_ph: feat_paths,
                  feat2_paths_ph: feat2_paths,
                  feat3_paths_ph: feat3_paths,
                  label_paths_ph: label_paths})

                # for each epoch
                batch_count = 1
                while True:
                    try:
                        ##################### Data loading ########################
                        start_time = time.time()
                        eve, eve_sensors, eve_segment, lab, batch_sess = sess.run(next_train)
                        # for memory concern, 1000 events are used in maximum
                        if eve.shape[0] > 1000:
                            idx = np.random.permutation(eve.shape[0])[:1000]
                            eve = eve[idx]
                            eve_sensors = eve_sensors[idx]
                            eve_segment = eve_segment[idx]
                            lab = lab[idx]
                            batch_sess = batch_sess[idx]
                        load_time = time.time() - start_time
    
                        ##################### Triplet selection #####################
                        start_time = time.time()
                        # Get the embeddings of all events
                        eve_embedding = np.zeros((eve.shape[0], cfg.emb_dim), dtype='float32')
                        for start, end in zip(range(0, eve.shape[0], cfg.batch_size),
                                            range(cfg.batch_size, eve.shape[0]+cfg.batch_size, cfg.batch_size)):
                            end = min(end, eve.shape[0])
                            emb = sess.run(embedding, feed_dict={input_ph: eve[start:end], dropout_ph: 1.0})
                            eve_embedding[start:end] = np.copy(emb)
    
                        # sample triplets within sampled sessions
                        all_diff = utils.all_diffs(eve_embedding, eve_embedding)
                        triplet_input_idx, active_count = utils.select_triplets_facenet(lab,utils.cdist(all_diff,metric=cfg.metric),cfg.triplet_per_batch,cfg.alpha,num_negative=cfg.num_negative)
                        if len(triplet_input_idx) == 0:
                            continue
                        
                        triplet_input = eve[triplet_input_idx]
                        sensors_input = eve_sensors[triplet_input_idx]
                        segment_input = eve_segment[triplet_input_idx]

                        select_time = time.time() - start_time

                        if len(triplet_input.shape) > 5:    # debugging
                            pdb.set_trace()
    
                        ##################### Start training  ########################
    
                        err, metric_err, hal_err, _, step, summ = sess.run(
                                [total_loss, metric_loss, hal_loss, train_op, global_step, summary_op],
                                feed_dict = {input_ph: triplet_input,
                                             input_sensors_ph: sensors_input,
                                             input_segment_ph: segment_input,
                                             dropout_ph: cfg.keep_prob,
                                             lr_ph: learning_rate})
    
                        print ("%s\tEpoch: [%d][%d/%d]\tEvent num: %d\tTriplet num: %d\tLoad time: %.3f\tSelect time: %.3f\tMetric Loss %.4f\tHal Loss %.4f" % \
                                (cfg.name, epoch+1, batch_count, batch_per_epoch, eve.shape[0], triplet_input.shape[0]//3, load_time, select_time, metric_err, hal_err))
    
                        summary = tf.Summary(value=[tf.Summary.Value(tag="train_loss", simple_value=err),
                                    tf.Summary.Value(tag="active_count", simple_value=active_count),
                                    tf.Summary.Value(tag="metric_loss", simple_value=metric_err),
                                    tf.Summary.Value(tag="hallucination_loss", simple_value=hal_err)])
    
                        summary_writer.add_summary(summary, step)
                        summary_writer.add_summary(summ, step)

                        batch_count += 1
                    
                    except tf.errors.OutOfRangeError:
                        print ("Epoch %d done!" % (epoch+1))
                        break

                # validation on val_set
                print ("Evaluating on validation set...")
                val_embeddings, hal_err, _ = sess.run([embedding, hal_loss, set_emb],
                                                feed_dict = {input_ph: val_feats,
                                                             input_sensors_ph: val_feats2,
                                                             input_segment_ph: val_feats3,
                                                             dropout_ph: 1.0})
                mAP, mPrec = utils.evaluate_simple(val_embeddings, val_labels)

                summary = tf.Summary(value=[tf.Summary.Value(tag="Valiation mAP", simple_value=mAP),
                                            tf.Summary.Value(tag="Validation mPrec@0.5", simple_value=mPrec),
                                            tf.Summary.Value(tag="Validation hal loss", simple_value=hal_err)])
                summary_writer.add_summary(summary, step)
                print ("Epoch: [%d]\tmAP: %.4f\tmPrec: %.4f" % (epoch+1,mAP,mPrec))

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
