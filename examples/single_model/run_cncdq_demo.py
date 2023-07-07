import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/../../")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from edustudio.quickstart import run_edustudio

run_edustudio(
    dataset='AAAI_2023',
    cfg_file_name=None,
    datatpl_cfg_dict={
        'cls': 'CNCDQDataTPL',
    },
    traintpl_cfg_dict={
        'cls': 'EduTrainTPL',
    },
    modeltpl_cfg_dict={
        'cls': 'CNCD_Q',
    },
    evaltpl_cfg_dict={
        'clses': ['BinaryClassificationEvalTPL'],
    }
)
