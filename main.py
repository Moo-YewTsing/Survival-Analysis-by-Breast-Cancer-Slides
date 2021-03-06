from snas import SNAS
from tools import gen_logger
import argparse
import os

if __name__ == "__main__":
    logger = gen_logger('main+')
    parse = argparse.ArgumentParser()
    parse.add_argument('i', help='the path of directory that saves imgs for cases')
    parse.add_argument('-e', default='data/Target.xlsx', help='the path of event/censor table')
    parse.add_argument('-o', default='..', help='the path for output')
    parse.add_argument('-r', type=float, default=0.8, help='training size')
    parse.add_argument('-m', type=str, default='', help='the path of trained weights')
    parse.add_argument('-t', type=int, default=40, help='epochs')
    parse.add_argument('-v', type=bool, default=False, help='validation only')
    parse.add_argument('-s', type=int, default=42, help='the num of imgs used for validation')
    parse.add_argument('-a', type=int, default=0, help='the time of augmentation during training')
    parse.add_argument('-p', type=bool, default=False, help='whether plot the model and save it in dst')
    parse.add_argument('-d', type=int, default=256, help="the size of model's key dense layer")
    parse.add_argument('-g', type=bool, default=False, help="use gene info to train")
    command = parse.parse_args()
    logger.info(f'Begin train on {command}')
    dst = command.o
    os.makedirs(dst, exist_ok=True)
    try:
        model = SNAS(command.i, dst, train_size_ratio=command.r,
        epochs=command.t, val_sel_num=command.s, aug_time=command.a, logger=logger,
        d_size=command.d, gene=command.g, target_p=command.e)
        # model.model.summary(print_fn=logger.info)
        if command.p:
            model.plot()
        if command.m:
            model.load(command.m)
        if model.trained and command.v:
            for _ in range(command.t):
                model.feedback()
        else:
            model.whole_train()
    except:
        logger.exception('something wrong')