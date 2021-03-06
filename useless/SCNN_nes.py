from keras.models import Model, Sequential
from keras.preprocessing.image import img_to_array, ImageDataGenerator
from keras.layers import Input, GlobalMaxPooling2D, GlobalAveragePooling2D, Flatten, Concatenate, Dropout, Dense, Conv2D, BatchNormalization, MaxPooling2D
from keras import backend as K
from keras.regularizers import l2
from keras.optimizers import Adagrad, RMSprop
from keras.applications.nasnet import NASNetMobile, preprocess_input
import os
import cv2
import random
import pandas as pd
import numpy as np
from tools import get_seq, gen_logger
import argparse
from lifelines.utils import concordance_index
from functools import lru_cache
random.seed(42)

models = {}
def get_model(func):
    models[func.__name__[-3:]] = func
    return func

@get_model
def model_pns():
    model = Sequential()
    model.add(Conv2D(64, (3, 3), activation='relu', padding='same', input_shape=(96, 96, 3)))
    model.add(BatchNormalization())
    model.add(MaxPooling2D())

    model.add(Conv2D(128, (3, 3), activation='relu', padding='same'))
    model.add(BatchNormalization())
    model.add(MaxPooling2D())

    model.add(Conv2D(256, (3, 3), activation='relu', padding='same'))
    model.add(BatchNormalization())
    model.add(MaxPooling2D())

    model.add(Flatten())
    model.add(Dense(128))
    model.add(Dropout(0.5))
    model.add(Dense(1, activation="linear", kernel_initializer='glorot_uniform', 
    kernel_regularizer=l2(0.01), activity_regularizer=l2(0.01)))
    model.summary()
    return model

@get_model
def model_nas():
    inputs = Input((96, 96, 3))
    base_model = NASNetMobile(include_top=False, input_shape=(96, 96, 3))#, weights=None
    x = base_model(inputs)
    out1 = GlobalMaxPooling2D()(x)
    out2 = GlobalAveragePooling2D()(x)
    out3 = Flatten()(x)
    out = Concatenate(axis=-1)([out1, out2, out3])
    out = Dropout(0.5)(out)
    out = Dense(256, kernel_initializer='glorot_uniform', 
    kernel_regularizer=l2(0.01), activity_regularizer=l2(0.01))(out)
    out = Dense(1, activation="linear", kernel_initializer='glorot_uniform', 
    kernel_regularizer=l2(0.01), activity_regularizer=l2(0.01))(out)
    model = Model(inputs, out)
    model.load_weights('model.h5', by_name='NASNet')
    model.layers[1].trainable = False
    model.summary()
    return model

def negative_log_likelihood(E):
	def loss(y_true,y_pred):
		hazard_ratio = K.exp(y_pred)
		log_risk = K.log(K.cumsum(hazard_ratio))
		uncensored_likelihood = K.transpose(y_pred) - log_risk
		censored_likelihood = uncensored_likelihood * E
		num_observed_event = K.sum([float(e) for e in E]) + 1
		return K.sum(censored_likelihood) / num_observed_event * (-1)
	return loss

def case2path(x_p):
    cur_dirs = os.listdir(x_p)
    result = {}
    for dir_n in cur_dirs:
        case = dir_n[:12]
        if case in result:
            continue
        result[case] = os.path.join(x_p, dir_n)
    return pd.DataFrame.from_dict(result, orient='index', columns=['path'])

def merge_table_creator(selected_p, target_p='data/Target.xlsx'):
    target = pd.read_excel(target_p)
    case_path_df = case2path(selected_p)
    merge_table = case_path_df.merge(target, left_index=True, right_on='sample')
    print(len(merge_table)/len(target))
    merge_table.reset_index(drop=True, inplace=True)
    return merge_table

def train_table_creator(merge_table):
    idx = merge_table.index.tolist()
    random.shuffle(idx)

    train_size = round(len(idx) * train_ratio)
    train_idx = idx[:train_size]
    test_idx = idx[train_size:]

    train_table = merge_table.iloc[train_idx]
    train_table.sort_values('duration', inplace=True)
    train_table.reset_index(drop=True, inplace=True)

    test_table = merge_table.iloc[test_idx]
    test_table.sort_values('duration', inplace=True)
    test_table.reset_index(drop=True, inplace=True)
    train_table.to_excel(train_table_p)
    test_table.to_excel(test_table_p)
    return train_table, test_table

@lru_cache(maxsize=759)
def get_pool(dir_p):
    return os.listdir(dir_p)

def read_train_dir(dir_p):
    pool = get_pool(dir_p)
    sel = random.choice(pool)
    x = os.path.join(dir_p, sel)
    return cv2.imread(x)

def read_val_dir(dir_p, sel_num=10, use_filter=False) -> list:
    pool = get_pool(dir_p)
    pool_size = len(pool)
    if use_filter and pool_size < sel_num:
        raise ValueError(f'the number ({pool_size}) in dir is not enough to have a reliable validation')
    sels = random.choices(pool, k=sel_num)
    xs = [os.path.join(dir_p, sel) for sel in sels]
    return [cv2.imread(x) for x in xs]

def chunk(df_sort, batch_size=64, epochs=10):
    population = list(range(len(df_sort)))
    for _ in range(epochs):
        chunk_idx = random.choices(population, k=batch_size)
        chunk_idx.sort()
        yield df_sort.iloc[chunk_idx]

def data_val(df_sort, sel_num=42, use_filter=False):
    X, T, E = [], [], []
    for item in df_sort.iterrows():
        path = item[1][0]
        dur = item[1][2]
        obs = item[1][3]
        try:
            X.append(read_val_dir(path, sel_num=sel_num, use_filter=use_filter))
        except ValueError:
            logger.warning(f'check imgs in {path}')
            continue
        else:
            T.append(dur)
            E.append(obs)
    T = np.array(T)
    return X, T, E

def data_gen_batch(df_sort, batch_size=64, epochs=10):
    for chunk_df in chunk(df_sort, batch_size=batch_size, epochs=epochs):
        X, T, E = [], [], []
        for item in chunk_df.iterrows():
            path = item[1][0]
            dur = item[1][2]
            obs = item[1][3]
            X.append(read_train_dir(path))
            T.append(dur)
            E.append(obs)
        T = np.array(T)
        yield X, T, E

def data_gen_whole(df_sort, epochs=10): # whole will not change T and E, so we can save resource
    X, T, E, paths = [], [], [], []
    for item in df_sort.iterrows():
        path = item[1][0]
        dur = item[1][2]
        obs = item[1][3]
        paths.append(path)
        X.append(read_train_dir(path))
        T.append(dur)
        E.append(obs)
    T = np.array(T)
    yield X, T, E

    for _ in range(epochs-1):
        X.clear()
        for path in paths:
            X.append(read_train_dir(path))
        yield X, T, E

def x_aug(X, time=100):
    for _ in range(time):
        X = seq.augment_images(X)
        X = [preprocess_input(x) for x in X]
        yield np.array(X)

def model_train_eval(model, X, y, e):
    X = [preprocess_input(x) for x in X]
    X = np.array(X)
    hr_preds = model.predict(X)
    hr_preds = np.exp(hr_preds)
    ci = concordance_index(y,-hr_preds,e)
    return ci

def model_val_eval(model, X_val, y, e):
    hr_preds = []
    for x_case in X_val:
        x_case = [preprocess_input(x) for x in x_case]
        x_case = np.array(x_case)
        hr_pred = model.predict(x_case)
        hr_pred = sorted(hr_pred)[-2] # only the second most serious area
        hr_preds.append(hr_pred)
    hr_preds = np.exp(hr_preds)
    ci = concordance_index(y,-hr_preds,e)
    return ci
    
def train_aux(model, X, Y, aug_time=20, event_size=None):
    for X in x_aug(X, time=aug_time):
        model.fit(
            X, Y,
            batch_size=event_size,
            epochs=22,
            verbose=False,
            shuffle=False)

def whole_train(model, train_table, test_table, epochs=40):
    # whole train can allocate more resources to train on varying imgs, so epochs is 40
    start = True
    event_size = None
    X_val, Y_val, E_val = data_val(test_table)
    for epoch, (X, Y, E) in enumerate(data_gen_whole(train_table, epochs=epochs)):
        if start:
            model.compile(loss=negative_log_likelihood(E), optimizer=ada)
            event_size = len(E)
            start = False
        train_aux(model, X, Y, aug_time=10, event_size=event_size)
        model.save_weights(f'{epoch}.h5')
        val_aux(model, epoch, X, Y, E, X_val, Y_val, E_val)

def batch_train(model, train_table, test_table, epochs=20, batch_size=64):
    event_size = batch_size
    X_val, Y_val, E_val = data_val(test_table)
    for epoch, (X, Y, E) in enumerate(data_gen_batch(train_table, batch_size=batch_size, epochs=epochs)):
        model.compile(loss=negative_log_likelihood(E), optimizer=ada)
        train_aux(model, X, Y, aug_time=20, event_size=event_size)
        val_aux(model, epoch, X, Y, E, X_val, Y_val, E_val)

def val_aux(model, epoch, X, Y, E, X_val, Y_val, E_val):
    try:
        logger.info(f'{epoch} -> train:{model_train_eval(model, X, Y, E)}; val:{model_val_eval(model, X_val, Y_val, E_val)}')
    except TypeError:
        logger.warning('some cases have problems')

def train(selected_p, model_name='pns', dst='..', trained=None, epochs=20, whole=True, only_val=False):
    if os.path.isfile(train_table_p) and os.path.isfile(test_table_p):
        print('Read from Caches')
        train_table = pd.read_excel(train_table_p)
        test_table = pd.read_excel(test_table_p)
    else:
        print('Create from Disk')
        merge_table = merge_table_creator(selected_p)
        train_table, test_table = train_table_creator(merge_table)
    model_creator = models.get(model_name, model_pns)
    model = model_creator()
    loaded = False
    if trained is not None:
        try:
            model.load_weights(trained)
        except:
            logger.warning('Wrong weight saving file')
        else:
            loaded = True
            model_name = os.path.splitext(os.path.basename(trained))[0] + '+'

    if only_val and loaded:
        X, Y, E = data_val(train_table, sel_num=42, use_filter=True)
        X_val, Y_val, E_val = data_val(test_table, sel_num=42, use_filter=True)
        logger.info(f'train:{model_val_eval(model, X, Y, E)} num:{len(Y)}; val:{model_val_eval(model, X_val, Y_val, E_val)} num:{len(Y_val)}')

    else:
        if whole:
            whole_train(model, train_table, test_table, epochs=epochs)
        else:
            batch_train(model, train_table, test_table, epochs=epochs, batch_size=64)

logger = gen_logger('train')
train_table_p = 'data/train_table.xlsx'
test_table_p = 'data/test_table.xlsx'
train_ratio = 0.5
if __name__ == "__main__":
    parse = argparse.ArgumentParser()
    parse.add_argument('i', help='the path of directory that saves imgs for cases')
    parse.add_argument('-o', default='..', help='the path for output')
    parse.add_argument('-n', default='nas', help='the model name (pns or nas)')
    parse.add_argument('-m', help='the path of trained weights')
    parse.add_argument('-t', type=int, default=40, help='epochs')
    parse.add_argument('-v', type=bool, default=False, help='validation only')
    command = parse.parse_args()
    try:
        logger.info(f'Begin train on {command}')
        ada = Adagrad(lr=1e-3, decay=0.1)
        seq = get_seq()
        dst = command.o
        os.makedirs(dst, exist_ok=True)
        train(command.i, dst=dst, model_name=command.n, trained=command.m, epochs=command.t, only_val=command.v)
    except:
        logger.exception('something wrong')