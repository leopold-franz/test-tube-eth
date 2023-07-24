import datetime
import os
import signal
import sys
import time
import traceback
from subprocess import call

from .argparse_hopt import HyperOptArgumentParser


def exit():
    time.sleep(1)
    os._exit(1)


class AbstractCluster(object):

    RUN_CMD = 'sbatch'
    def __init__(
            self,
            hyperparam_optimizer=None,
            log_path=None,
            python_cmd='python3',
            enable_log_err=True,
            enable_log_out=True,
            stage='train'
    ):
        self.hyperparam_optimizer = hyperparam_optimizer
        self.log_path = log_path

        self.enable_log_err = enable_log_err
        self.enable_log_out = enable_log_out
        self.slurm_files_log_path = None
        self.err_log_path = None
        self.out_log_path = None
        self.modules = []
        self.script_name = os.path.realpath(sys.argv[0])
        self.job_time = '04:00:00'
        self.per_experiment_nb_gpus = 1
        self.per_experiment_nb_cpus = 1
        self.per_experiment_nb_nodes = 1
        self.memory_mb_per_node = 2000
        self.memory_mb_per_cpu = 1024  # Can also be "10G" corresponding to 10GB
        self.email = None
        self.notify_on_end = False
        self.notify_on_fail = False
        self.job_name = None
        self.python_cmd = python_cmd
        self.gpu_type = None
        self.on_gpu = False
        self.call_load_checkpoint = False
        self.commands = []
        self.slurm_commands = []
        self.hpc_exp_number = 0
        self.stage = stage

    def add_slurm_cmd(self, cmd, value, comment):
        self.slurm_commands.append((cmd, value, comment))

    def add_command(self, cmd):
        self.commands.append(cmd)

    def load_modules(self, modules):
        self.modules = modules

    def notify_job_status(self, email, on_done, on_fail):
        self.email = email
        self.notify_on_end = on_done
        self.notify_on_fail = on_fail

    def optimize_parallel_cluster(self, train_function, nb_trials, job_name):
        raise NotImplementedError

    def optimize_parallel_slurm(self, job_name, output_file, error_file, job_time, nb_gpus, nb_nodes, memory, notifications_email, gpu_types):
        pass


class SlurmCluster(AbstractCluster):
    def __init__(self, *args, **kwargs):
        super(SlurmCluster, self).__init__(*args, **kwargs)

    def optimize_parallel_cluster_gpu(
            self,
            pytorch_cli_path,
            base_config_path,
            job_name,
            nb_trials=None,
            enable_auto_resubmit=False,
            job_display_name=None
    ):
        if job_display_name is None:
            job_display_name = job_name

        self.__optimize_parallel_cluster_internal(pytorch_cli_path, base_config_path, nb_trials, job_name, job_display_name,
                                                  enable_auto_resubmit, on_gpu=True)

    def optimize_parallel_cluster_cpu(
            self,
            pytorch_cli_path,
            base_config_path,
            nb_trials,
            job_name,
            enable_auto_resubmit=False,
            job_display_name=None
    ):
        if job_display_name is None:
            job_display_name = job_name

        self.__optimize_parallel_cluster_internal(pytorch_cli_path, base_config_path, nb_trials, job_name, job_display_name,
                                                  enable_auto_resubmit, on_gpu=False)

    def __optimize_parallel_cluster_internal(
            self,
            pytorch_cli_path,
            base_config_path,
            nb_trials,
            job_name,
            job_display_name,
            enable_auto_resubmit,
            on_gpu
    ):
        """
        Runs optimization on the attached cluster
        :param pytorch_cli_path:
        :param nb_trials:
        :param job_name:
        :return:
        """
        self.job_name = job_name
        self.job_display_name = job_display_name
        self.on_gpu = on_gpu

        # layout logging structure
        self.__layout_logging_dir()

        # Launcher script. Generate trials and launch jobs.

        # generate hopt trials
        trials = self.hyperparam_optimizer.generate_trials(nb_trials)

        # get the max test tube exp version so far if it's there
        if self.enable_log_out:
            scripts_path = os.path.join(self.log_path, 'slurm_out_logs')
            next_trial_version = self.__get_max_trial_version(scripts_path)
        else:
            next_trial_version = 0

        # for each trial, generate a slurm command
        for i, trial_params in enumerate(trials):
            exp_i = i + next_trial_version
            self.schedule_experiment(pytorch_cli_path, base_config_path, trial_params, exp_i, enable_auto_resubmit)

    def schedule_experiment(self, pytorch_cli_path, base_config_path, trial_params, exp_i, enable_auto_resubmit):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
        timestamp = 'trial_{}_{}'.format(exp_i, timestamp)

        # generate command
        slurm_cmd_script_path = os.path.join(self.slurm_files_log_path, '{}_slurm_cmd.sh'.format(timestamp))
        slurm_cmd = self.__build_slurm_command(pytorch_cli_path, base_config_path, trial_params, slurm_cmd_script_path, timestamp, exp_i, self.on_gpu, enable_auto_resubmit)
        self.__save_slurm_cmd(slurm_cmd, slurm_cmd_script_path)

        # run script to launch job
        print('\nlaunching exp...')
        result = call(f'{AbstractCluster.RUN_CMD} {slurm_cmd_script_path}', shell=True)
        if result == 0:
            print('launched exp ', slurm_cmd_script_path)
        else:
            print('launch failed...')

    def __save_slurm_cmd(self, slurm_cmd, slurm_cmd_script_path):
        print('saving slurm cmd to ', slurm_cmd_script_path)
        with open(slurm_cmd_script_path, mode='w') as file:
            file.write(slurm_cmd)

    def __get_max_trial_version(self, path):
        files = os.listdir(path)
        version_files = [f for f in files if 'trial_' in f]
        if len(version_files) > 0:
            # regex out everything except file version for ve
            versions = [int(f_name.split('_')[1]) for f_name in version_files]
            max_version = max(versions)
            return max_version + 1
        else:
            return 0

    def __layout_logging_dir(self):
        """
        Generates dir structure for logging errors and outputs
        :return:
        """
        print("Setting up slurm loggers to {}".format(self.log_path))
        # format the logging folder path
        slurm_out_path = os.path.join(self.log_path, self.job_name)

        self.log_path = slurm_out_path

        # if we have a test tube name, make the folder and set as the logging destination
        if not os.path.exists(slurm_out_path):
            os.makedirs(slurm_out_path)

        # when err logging is enabled, build add the err logging folder
        if self.enable_log_err:
            err_path = os.path.join(slurm_out_path, 'slurm_err_logs')
            if not os.path.exists(err_path):
                os.makedirs(err_path)
            self.err_log_path = err_path

        # when out logging is enabled, build add the out logging folder
        if self.enable_log_out:
            out_path = os.path.join(slurm_out_path, 'slurm_out_logs')
            if not os.path.exists(out_path):
                os.makedirs(out_path)
            self.out_log_path = out_path

        # place where slurm files log to
        self.slurm_files_log_path = os.path.join(slurm_out_path, 'slurm_scripts')
        if not os.path.exists(self.slurm_files_log_path):
            os.makedirs(self.slurm_files_log_path)

    def __get_hopt_params(self, trial):
        """
        Turns hopt trial into script params
        :param trial:
        :return:
        """

        params = []
        for k in trial.__dict__:
            v = trial.__dict__[k]

            # don't add None params
            if v is None or v is False:
                continue

            # put everything in quotes except bools
            if self.__should_escape(v):
                cmd = '--{} \"{}\"'.format(k, v)
            else:
                cmd = '--{} {}'.format(k, v)
            params.append(cmd)

        full_cmd = ' '.join(params)
        return full_cmd

    def __should_escape(self, v):
        v = str(v)
        return '[' in v or ';' in v or ' ' in v

    def __build_slurm_command(self, pytorch_cli_path, base_config_path, trial, slurm_cmd_script_path, timestamp, exp_i, on_gpu, enable_auto_resubmit):
        sub_commands = []

        command =[
            '#!/bin/bash -l',
            '#',
            '# Auto-generated by test-tube (https://github.com/williamFalcon/test-tube)',
            '#################\n'
        ]
        sub_commands.extend(command)

        # add job name
        job_with_version = '{}v{}'.format(self.job_display_name, exp_i)
        command = [
            '# set a job name',
            '#SBATCH --job-name={}'.format(job_with_version),
            '#################\n',
        ]
        sub_commands.extend(command)

        # add out output
        if self.enable_log_out:
            out_path = os.path.join(self.out_log_path, '{}_slurm_output_%j.out'.format(timestamp))
            command = [
                '# a file for job output, you can check job progress',
                '#SBATCH --output={}'.format(out_path),
                '#################\n',
            ]
            sub_commands.extend(command)

        # add err output
        if self.enable_log_err:
            err_path = os.path.join(self.err_log_path, '{}_slurm_output_%j.err'.format(timestamp))
            command = [
                '# a file for errors',
                '#SBATCH --error={}'.format(err_path),
                '#################\n',
            ]
            sub_commands.extend(command)

        # add job time
        command = [
            '# time needed for job',
            '#SBATCH --time={}'.format(self.job_time),
            '#################\n'
        ]
        sub_commands.extend(command)

        # add nb of gpus
        if self.per_experiment_nb_gpus > 0 and on_gpu:
            command = [
                '# gpus per node',
                '#SBATCH --gpus={}'.format(self.per_experiment_nb_gpus),
                '#################\n'
            ]
            if self.gpu_type is not None:
                command = [
                    '# gpus per node',
                    '#SBATCH --gpus={}:{}'.format(self.gpu_type, self.per_experiment_nb_gpus),
                    '#################\n'
                ]
            sub_commands.extend(command)

        # add nb of cpus if not looking at a gpu job
        if self.per_experiment_nb_cpus > 0:
            command = [
                '# cpus per job',
                '#SBATCH --cpus-per-task={}'.format(self.per_experiment_nb_cpus),
                '#################\n'
            ]
            sub_commands.extend(command)

        # pick nb nodes
        command = [
            '# number of requested nodes',
            '#SBATCH --nodes={}'.format(self.per_experiment_nb_nodes),
            '#################\n'
        ]
        sub_commands.extend(command)

        # pick memory per node
        # command = [
        #     '# memory per node',
        #     '#SBATCH --mem={}'.format(self.memory_mb_per_node),
        #     '#################\n'
        # ]
        # sub_commands.extend(command)

        # pick memory per task
        command = [
            '# memory per task',
            '#SBATCH --mem-per-cpu={}'.format(self.memory_mb_per_cpu),
            '#################\n'
        ]
        sub_commands.extend(command)

        # add signal command to catch job termination
        if enable_auto_resubmit:
            command = [
                '# slurm will send a signal this far out before it kills the job',
            f'#SBATCH --signal=SIGHUP@90',
            '#################\n'
        ]

            sub_commands.extend(command)

        # Subscribe to email if requested
        mail_type = []
        if self.notify_on_end:
            mail_type.append('END')
        if self.notify_on_fail:
            mail_type.append('FAIL')
        if len(mail_type) > 0:
            mail_type_query = [
                '# Have SLURM send you an email when the job ends or fails',
                '#SBATCH --mail-type={}'.format(','.join(mail_type))
            ]
            sub_commands.extend(mail_type_query)

            email_query = [
                '#SBATCH --mail-user={}'.format(self.email),
            ]
            sub_commands.extend(email_query)

        # add custom sbatch commands
        sub_commands.append('\n')
        for (cmd, value, comment) in self.slurm_commands:
            comment = '# {}'.format(comment)
            cmd = '#SBATCH --{}={}'.format(cmd, value)
            spaces = '#################\n'
            sub_commands.extend([comment, cmd, spaces])

        # load modules
        sub_commands.append('\n')
        for module in self.modules:
            cmd = 'module load {}'.format(module)
            sub_commands.append(cmd)

        # remove spaces before the hash
        sub_commands = [x.lstrip() for x in sub_commands]

        # add additional commands
        for cmd in self.commands:
            sub_commands.append(cmd)
            sub_commands.append('\n')

        # add run command
        trial_args = self.__get_hopt_params(trial)

        cmd = f'srun {self.python_cmd} {pytorch_cli_path} {self.stage} --config={base_config_path} {trial_args}'
        sub_commands.append(cmd)

        # build full command with empty lines in between
        full_command = '\n'.join(sub_commands)
        return full_command
