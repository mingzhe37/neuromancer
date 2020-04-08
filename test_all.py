import os

for ssm in ['vanilla', 'pf', 'svd', 'spectral', 'true']:
    for heatflow in ['black', 'grey', 'white']:
        for state_estimator in ['true', 'linear', 'pf', 'mlp', 'rnn', 'rnn_constr', 'rnn_spectral', 'rnn_svd', 'kf']:
            print(f'\n###############\n{ssm} {heatflow} {state_estimator}\n#################\n')
            err = os.system(f'python train.py -ssm_type {ssm} -heatflow {heatflow} -state_estimator {state_estimator}')
            if err != 0:
                with open('test_all.log', 'a') as log:
                    log.write(f'{ssm} {heatflow} {state_estimator}\n')