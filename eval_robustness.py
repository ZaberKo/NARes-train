import mlconfig
import argparse
import datetime
import util
import models
import dataset
import time
import os
import torch
import shutil
import numpy as np
from evaluator import Evaluator
from torch.autograd import Variable
from torchprofile import profile_macs

mlconfig.register(dataset.DatasetGenerator)

# python ./eval_robustness.py --config_path /research/hal-huangs88/codes/Robust_NASBench/ablation_dir/nasbench/small/r1 --attack_choice PGD --load_best_model  --version arch_001
parser = argparse.ArgumentParser(description='RobustArc')
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--version', type=str, default="DARTS_Search")
parser.add_argument('--config_path', type=str, default='configs')
parser.add_argument('--load_model', action='store_true', default=False)
parser.add_argument('--load_best_model', action='store_true', default=False)
parser.add_argument('--attack_choice', default='PGD',choices=['PGD', 'AA', 'GAMA', 'CW', 'none', 'FGSM', 'MI-FGSM',
                                                              "TI-FGSM", "l2-pgd", "L2-AA"])
parser.add_argument('--epsilon', default=8, type=float, help='perturbation')
parser.add_argument('--num_steps', default=20, type=int, help='perturb number of steps')
parser.add_argument('--step_size', default=0.8, type=float, help='perturb step size')


args = parser.parse_args()
if args.epsilon > 1:
    args.epsilon = args.epsilon / 255
    args.step_size = args.step_size / 255

log_file_path = os.path.join(args.config_path, args.version)
checkpoint_path = "{}/checkpoints".format(args.config_path)
checkpoint_path_file = os.path.join(checkpoint_path, args.version)
config_file = os.path.join(args.config_path, args.version) + '.yaml'
config = mlconfig.load(config_file)
config['dataset']['valset'] = False
config['dataset']['eval_batch_size'] = 100

logger = util.setup_logger(name=args.version, log_file=log_file_path + '_eval@{}-{}steps'.format(args.attack_choice, args.num_steps) + ".log")

torch.manual_seed(args.seed)
np.random.seed(args.seed)

if torch.cuda.is_available():
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    device = torch.device('cuda')
    device_list = [torch.cuda.get_device_name(i) for i in range(0, torch.cuda.device_count())]
    logger.info("GPU List: %s" % (device_list))
else:
    device = torch.device('cpu')


def whitebox_eval(data_loader, model, evaluator, log=True):
    natural_count, pgd_count, total, stable_count = 0, 0, 0, 0
    loss_meters = util.AverageMeter()
    lip_meters = util.AverageMeter()
    print("    ###    Using {} ####    ".format(args.attack_choice))
    model.eval()
    for i, (images, labels) in enumerate(data_loader["test_dataset"]):
        images, labels = images.to(device), labels.to(device)
        # pgd attack
        images, labels = Variable(images, requires_grad=True), Variable(labels)
        if args.attack_choice == 'PGD':
            rs = evaluator._pgd_whitebox(model, images, labels, random_start=True,
                                         epsilon=args.epsilon, num_steps=args.num_steps,
                                         step_size=args.step_size)
        elif args.attack_choice == 'GAMA':
            rs = evaluator._gama_whitebox(model, images, labels, epsilon=args.epsilon,
                                          num_steps=args.num_steps, eps_iter=args.step_size)
        elif args.attack_choice == 'CW':
            rs = evaluator._pgd_cw_whitebox(model, images, labels, random_start=True,
                                        epsilon=args.epsilon, num_steps=args.num_steps,
                                        step_size=args.step_size)
        elif args.attack_choice == 'FGSM':
            rs = evaluator._fgsm_whitebox(model, images, labels, random_start=True,
                                        epsilon=args.epsilon, num_steps=args.num_steps,
                                        step_size=args.step_size)
        else:
            raise ('Not implemented')
        acc, acc_pgd, loss, stable, X_pgd = rs
        total += images.size(0)
        natural_count += acc
        pgd_count += acc_pgd
        stable_count += stable
        local_lip = util.local_lip(model, images, X_pgd).item()
        lip_meters.update(local_lip)
        loss_meters.update(loss)
        if log:
            payload = 'LIP: %.4f\tStable Count: %d\tNatural Count: %d/%d\tNatural Acc: ' \
                      '%.2f\tAdv Count: %d/%d\tAdv Acc: %.2f' % (
                          local_lip, stable_count, natural_count, total, (natural_count / total) * 100,
                          pgd_count, total, (pgd_count / total) * 100)
            logger.info(payload)
    natural_acc = (natural_count / total) * 100
    pgd_acc = (pgd_count / total) * 100
    payload = 'Natural Correct Count: %d/%d Acc: %.2f ' % (natural_count, total, natural_acc)
    logger.info(payload)
    payload = '%s Correct Count: %d/%d Acc: %.2f ' % (args.attack_choice, pgd_count, total, pgd_acc)
    logger.info(payload)
    payload = '%s with %.1f steps Loss Avg: %.2f ' % (args.attack_choice, args.num_steps, loss_meters.avg)
    logger.info(payload)
    payload = 'LIP Avg: %.4f ' % (lip_meters.avg)
    logger.info(payload)
    payload = 'Stable Count: %d/%d StableAcc: %.2f ' % (stable_count, total, stable_count * 100 / total)
    logger.info(payload)
    return natural_acc, pgd_acc, stable_count * 100 / total, lip_meters.avg


def main():
    # Load Search Version Genotype
    model = config.model().to(device)
    logger.info(model)

    # Setup ENV
    data_loader = config.dataset().getDataLoader()
    evaluator = Evaluator(data_loader, logger, config)
    profile_inputs = (torch.randn([1, 3, 32, 32]).to(device),)
    flops = profile_macs(model, profile_inputs) / 1e6

    config.set_immutable()
    for key in config:
        logger.info("%s: %s" % (key, config[key]))
    logger.info("param size = %fMB", util.count_parameters_in_MB(model))
    logger.info("flops: %.4fM" % flops)
    logger.info("PyTorch Version: %s" % (torch.__version__))
    if torch.cuda.is_available():
        device_list = [torch.cuda.get_device_name(i) for i in range(0, torch.cuda.device_count())]
        logger.info("GPU List: %s" % (device_list))

    ENV = {'global_step': 0,
           'best_acc': 0.0,
           'curren_acc': 0.0,
           'best_pgd_acc': 0.0,
           'flops': flops,
           'train_history': [],
           'eval_history': [],
           'pgd_eval_history': [],
           'stable_acc_history': [],
           'lip_history': [],
           'genotype_list': []}

    if args.load_model or args.load_best_model:
        filename = checkpoint_path_file + '_best.pth' if args.load_best_model else checkpoint_path_file + '.pth'
        checkpoint = util.load_model(filename=filename,
                                     model=model,
                                     optimizer=None,
                                     alpha_optimizer=None,
                                     scheduler=None)
        ENV = checkpoint['ENV']
        if 'stable_acc_history' not in ENV:
            ENV['stable_acc_history'] = []
        if 'lip_history' not in ENV:
            ENV['lip_history'] = []
        logger.info("File %s loaded!" % (filename))

    model = torch.nn.DataParallel(model).to(device)
    if args.attack_choice in ['PGD', 'GAMA', 'CW', "MI-FGSM", "TI-FGSM", "FGSM"]:
        for param in model.parameters():
            param.requires_grad = False
        model.eval()
        natural_acc, adv_acc, stable_acc, lip = whitebox_eval(data_loader, model, evaluator)
        key = '%s_%d' % (args.attack_choice, args.num_steps)
        ENV['natural_acc'] = natural_acc
        ENV[key] = adv_acc
        ENV['%s_stable' % key] = stable_acc
        ENV['%s_lip' % key] = lip

    return


if __name__ == '__main__':
    for arg in vars(args):
        logger.info("%s: %s" % (arg, getattr(args, arg)))
    start = time.time()
    main()
    end = time.time()
    cost = (end - start) / 86400
    payload = "Running Cost %.2f Days" % cost
    logger.info(payload)
