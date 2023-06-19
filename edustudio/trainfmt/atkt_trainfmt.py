from .gd_trainfmt import GDTrainFmt
from edustudio.utils.common import UnifyConfig, set_same_seeds, tensor2npy
from edustudio.utils.callback import ModelCheckPoint, EarlyStopping, History, BaseLogger, Callback, CallbackList
from edustudio.model import BaseModel
import torch
from torch.optim import lr_scheduler
from torch.autograd import Variable, grad
from typing import Sequence
from collections import defaultdict
from tqdm import tqdm
import numpy as np
import shutil


class KTLoss(torch.nn.Module):

    def __init__(self):
        super(KTLoss, self).__init__()

    def forward(self, pred_answers, real_answers, mask_seq):

        real_answers = real_answers[:, 1:]
        answer_mask = mask_seq.long()
        
        y_pred = pred_answers[answer_mask].float()
        y_true = real_answers[answer_mask].float()
        
        loss=torch.nn.BCELoss()(y_pred, y_true)
        return loss, y_pred, y_true

def _l2_normalize_adv(d):
    if isinstance(d, Variable):
        d = d.data.cpu().numpy()
    elif isinstance(d, torch.FloatTensor) or isinstance(d, torch.cuda.FloatTensor):
        d = d.cpu().numpy()
    d /= (np.sqrt(np.sum(d ** 2, axis=(1, 2))).reshape((-1, 1, 1)) + 1e-16)
    return torch.from_numpy(d)
    
class AtktTrainFmt(GDTrainFmt):
    default_cfg = {
        'batch_size': 64,
        'eval_batch_size': 64,
        'num_stop_rounds': 10,
        'early_stop_metrics': [('auc','max')],
        'best_epoch_metric': 'auc',
        'unsave_best_epoch_pth': True,
        'ignore_metrics_in_train': [],
        'lr_decay': 50,
        'gamma': 0.5,
        'epsilon': 10,
        'beta': 0.2
    }

    def __init__(self, cfg: UnifyConfig):
        super().__init__(cfg)
        
    def _check_params(self):
        super()._check_params()
        assert self.trainfmt_cfg['best_epoch_metric'] in set(i[0] for i in self.trainfmt_cfg['early_stop_metrics'])

    def start(self):
        super().start()

        # callbacks
        num_stop_rounds = self.trainfmt_cfg['num_stop_rounds']
        es_metrics = self.trainfmt_cfg["early_stop_metrics"]
        modelCheckPoint = ModelCheckPoint(
            es_metrics, save_folder_path=f"{self.frame_cfg.temp_folder_path}/pths/"
        )
        earlystopping = EarlyStopping(es_metrics, num_stop_rounds=num_stop_rounds, start_round=1)
        callbacks = [
            modelCheckPoint, earlystopping, History(folder_path=f"{self.frame_cfg.temp_folder_path}/history/", plot_curve=True), 
            BaseLogger(self.logger, group_by_contains=['loss'])
        ]
        self.callback_list = CallbackList(callbacks=callbacks, model=self.model, logger=self.logger)
        # evalfmts
        for evalfmt in self.evalfmts: 
            evalfmt.set_callback_list(self.callback_list)
            evalfmt.set_dataloaders(self.train_loader, self.val_loader, self.test_loader)

        # train
        set_same_seeds(self.trainfmt_cfg['seed'])
        if self.val_loader is not None:
            self.fit(train_loader=self.train_loader, val_loader=self.val_loader)
        else:
            self.fit(train_loader=self.train_loader, val_loader=self.test_loader)
        
        if self.val_loader is not None:
            # load best params
            metric_name = self.trainfmt_cfg['best_epoch_metric']
            metric = [m for m in modelCheckPoint.metric_list if m.name == metric_name][0]
            fpth =  f"{self.frame_cfg.temp_folder_path}/pths/best-epoch-{metric.best_epoch:03d}-for-{metric.name}.pth"
            self.model.load_state_dict(torch.load(fpth))
            metrics = self.inference(self.test_loader)
            for name in metrics: self.logger.info(f"{name}: {metrics[name]}")
            History.dump_json(metrics, f"{self.frame_cfg.temp_folder_path}/result.json")

        if self.trainfmt_cfg['unsave_best_epoch_pth']: shutil.rmtree(f"{self.frame_cfg.temp_folder_path}/pths/")

    def fit(self, train_loader, val_loader):
        kt_loss = KTLoss()
        self.model.train()
        self.optimizer = self._get_optim()
        self.scheduler = lr_scheduler.StepLR(self.optimizer, step_size=self.trainfmt_cfg['lr_decay'], gamma=self.trainfmt_cfg['gamma'])
        self.callback_list.on_train_begin()
        for epoch in range(self.trainfmt_cfg['epoch_num']):
            self.callback_list.on_epoch_begin(epoch + 1)
            logs = defaultdict(lambda: np.full((len(train_loader),), np.nan, dtype=np.float32))
            for batch_id, batch_dict in enumerate(
                    tqdm(train_loader, ncols=self.frame_cfg['TQDM_NCOLS'], desc="[EPOCH={:03d}]".format(epoch + 1))
            ):
                batch_dict = self.batch_dict2device(batch_dict)
                pred_res, features = self.model(**batch_dict)
                loss, y_pred, y_true = kt_loss(pred_res, batch_dict['label_seq'], batch_dict['mask_seq'])

                features_grad = grad(loss, features, retain_graph=True)  # 返回loss对features的梯度
                p_adv = torch.FloatTensor(self.trainfmt_cfg['epsilon'] * _l2_normalize_adv(features_grad[0].data))
                p_adv = Variable(p_adv)
                pred_res, features = self.model(**batch_dict, p_adv = p_adv)
                adv_loss, _ , _ = kt_loss(pred_res, batch_dict['label_seq'], batch_dict['mask_seq'])

                total_loss = loss + self.trainfmt_cfg['beta']*adv_loss
                self.optimizer.zero_grad()
                total_loss.backward()
                self.optimizer.step()

                loss_dict = {'total_loss': total_loss, 'loss': loss, 'adv_loss': adv_loss}
                for k in loss_dict: logs[k][batch_id] = loss_dict[k].item() if loss_dict[k] is not None else np.nan
            
            self.scheduler.step()

            for name in logs: logs[name] = float(np.nanmean(logs[name]))

            if val_loader is not None:
                val_metrics = self.evaluate(val_loader)
                logs.update({f"{metric}": val_metrics[metric] for metric in val_metrics})

            self.callback_list.on_epoch_end(epoch + 1, logs=logs)
            if self.model.share_callback_dict.get('stop_training', False):
                break
            
        self.callback_list.on_train_end()
    
    @torch.no_grad()
    def evaluate(self, loader):
        self.model.eval()
        pd_list = list(range(len(loader)))
        gt_list = list(range(len(loader)))
        for idx, batch_dict in enumerate(tqdm(loader, ncols=self.frame_cfg['TQDM_NCOLS'], desc="[PREDICT]")):
            batch_dict = self.batch_dict2device(batch_dict)
            eval_dict = self.model.predict(**batch_dict)
            pd_list[idx] = eval_dict['y_pd']
            gt_list[idx] = eval_dict['y_gt']
        y_pd = torch.hstack(pd_list)
        y_gt = torch.hstack(gt_list)

        eval_data_dict = {
            'y_pd': y_pd,
            'y_gt': y_gt,
        }
        if hasattr(self.model, 'get_stu_status'):
            eval_data_dict.update({
                'stu_stats': tensor2npy(self.model.get_stu_status()),
            })
        if hasattr(loader.dataset, 'Q_mat'):
            eval_data_dict.update({
                'Q_mat': tensor2npy(loader.dataset.Q_mat)
            })
        eval_result = {}
        for evalfmt in self.evalfmts: eval_result.update(
                evalfmt.eval(ignore_metrics=self.trainfmt_cfg['ignore_metrics_in_train'], **eval_data_dict)
            )
        return eval_result

    @torch.no_grad()
    def inference(self, loader):
        self.model.eval()
        pd_list = list(range(len(loader)))
        gt_list = list(range(len(loader)))
        for idx, batch_dict in enumerate(tqdm(loader, ncols=self.frame_cfg['TQDM_NCOLS'], desc="[PREDICT]")):
            batch_dict = self.batch_dict2device(batch_dict)
            eval_dict = self.model.predict(**batch_dict)
            pd_list[idx] = eval_dict['y_pd']
            gt_list[idx] = eval_dict['y_gt']
        y_pd = torch.hstack(pd_list)
        y_gt = torch.hstack(gt_list)

        eval_data_dict = {
            'y_pd': y_pd,
            'y_gt': y_gt,
        }
        if hasattr(self.model, 'get_stu_status'):
            eval_data_dict.update({
                'stu_stats': tensor2npy(self.model.get_stu_status()),
            })
        if hasattr(loader.dataset, 'Q_mat'):
            eval_data_dict.update({
                'Q_mat': tensor2npy(loader.dataset.Q_mat)
            })
        eval_result = {}
        for evalfmt in self.evalfmts: eval_result.update(evalfmt.eval(**eval_data_dict))
        return eval_result