from multiprocessing import Pool
import os
import glob
import shlex
import pickle
import datetime

import b2luigi as luigi
from b2luigi.basf2_helper.tasks import Basf2PathTask
from b2luigi.basf2_helper.utils import get_basf2_git_hash
from b2luigi.batch.processes.gbasf2 import run_with_gbasf2

from B_generic_train import create_fei_path, get_particles
import fei


def shell_command(cmd):
    os.system(cmd)


# ballpark CPU times for the FEI stages executed on grid. Format: <stage-number> : <minutes>
grid_cpu_time = {
    -1: 10,
    0: 20,
    1: 30,
    2: 40,
    3: 8 * 60,
    4: 16 * 60,
    5: 24 * 60,
    6: 36 * 60,
}

fei_analysis_outputs = {}
fei_analysis_outputs[-1] = ["mcParticlesCount.root"]
for i in range(6):
    fei_analysis_outputs[i] = ["training_input.root"]
fei_analysis_outputs[6] = [
    "Monitor_FSPLoader.root",
    "Monitor_Final.root",
    "Monitor_ModuleStatistics.root",
    "Monitor_PostReconstruction_AfterMVA.root",
    "Monitor_PostReconstruction_AfterRanking.root",
    "Monitor_PostReconstruction_BeforePostCut.root",
    "Monitor_PostReconstruction_BeforeRanking.root",
    "Monitor_PreReconstruction_AfterRanking.root",
    "Monitor_PreReconstruction_AfterVertex.root",
    "Monitor_PreReconstruction_BeforeRanking.root",
]


class FEIAnalysisTask(Basf2PathTask):

    batch_system = "gbasf2"

    git_hash = luigi.Parameter(hashed=True, default=get_basf2_git_hash(), significant=False)

    gbasf2_project_name_prefix = luigi.Parameter(significant=False)
    gbasf2_input_dslist = luigi.Parameter(hashed=True, significant=False)

    cache = luigi.IntParameter(significant=False)
    monitor = luigi.BoolParameter(significant=False)
    stage = luigi.IntParameter()
    mode = luigi.Parameter()

    def output(self):

        for outname in fei_analysis_outputs[self.stage]:
            yield self.add_to_output(outname)

    def requires(self):

        if self.stage == -1:

            return []  # default implementation, as in the luigi.Task (if no requirements are present)

        else:

            # need the timestamp of the additional inputs to build their TMP-SE path
            yield PrepareInputsTask(
                mode="AnalysisInput",
                stage=self.stage - 1,
                remote_tmp_directory=luigi.get_setting("remote_tmp_directory"),
                remote_initial_se=luigi.get_setting("remote_initial_se"),
            )

            # need a symlink to the merged mcParticlesCount.root file
            yield MergeOutputsTask(
                mode="Merging",
                stage=-1,
                ncpus=luigi.get_setting("local_cpus"),
            )

            # need symlinks to *.xml files of FEI training of previous stages
            for fei_stage in range(self.stage):

                yield FEITrainingTask(
                    mode="Training",
                    stage=fei_stage,
                )

    def create_path(self):

        luigi.set_setting("gbasf2_cputime", grid_cpu_time[self.stage])

        # determine the remote TMP-SE destination of input tarball for gbasf2 command
        if self.stage > -1:
            timestamp = open(f"{self.get_input_file_names('successfull_input_upload.txt')[0]}", "r").read().strip()
            additional_file = os.path.join(luigi.get_setting("remote_tmp_directory").rstrip('/')+timestamp,
                                           "stage"+str(self.stage - 1), "sub00", "fei_analysis_inputs.tar.gz")
            luigi.set_setting("gbasf2_input_datafiles", [additional_file])

            # create symlinks to files, which are needed for current FEI analysis stage
            for key in self.get_input_file_names():
                if key.endswith(".xml"):
                    filepath = '"' + self.get_input_file_names(key)[0] + '"'
                    adjusted_key = '"' + key + '"'
                    os.system(f"ln -sf {filepath} {adjusted_key}")
            # need extra line for mcParticlesCount.root symlink, since removed otherwise
            os.system(f"ln -sf {self.get_input_file_names('mcParticlesCount.root')[0]} mcParticlesCount.root")

        path = create_fei_path(filelist=[], cache=self.cache, monitor=self.monitor)

        if self.stage > -1:
            # remove symlinks and not needed Summary.pickle files
            for key in self.get_input_file_names():
                if key == "mcParticlesCount.root" or key.endswith(".xml"):
                    adjusted_key = '"' + key + '"'
                    os.system(f"rm {adjusted_key}")
            os.system("rm -f Summary.pickle*")
        return path


class MergeOutputsTask(luigi.Task):

    ncpus = luigi.IntParameter(significant=False)  # to be used with setting 'local_cpus' in settings.json
    stage = luigi.IntParameter()
    mode = luigi.Parameter()

    def output(self):

        for outname in fei_analysis_outputs[self.stage]:
            yield self.add_to_output(outname)

    def requires(self):

        cache = -1 if self.stage == -1 else 0
        monitor = True if self.stage == 6 else False
        yield FEIAnalysisTask(
            cache=cache,
            monitor=monitor,
            mode="TrainingInput",
            stage=self.stage,
            gbasf2_project_name_prefix=luigi.get_setting("gbasf2_project_name_prefix"),
            gbasf2_input_dslist=luigi.get_setting("gbasf2_input_dslist"),
        )

    def run(self):

        cmds = []
        for inname in fei_analysis_outputs[self.stage]:
            # for some reason, only a one-element list: self.get_input_file_names(inname)
            cmds.append(f"analysis-fei-mergefiles {self.get_output_file_name(inname)} " +
                        " ".join(glob.glob(os.path.join(self.get_input_file_names(inname)[0], "*.root"))))

        p = Pool(self.ncpus)
        p.map(shell_command, cmds)


class FEITrainingTask(luigi.Task):

    stage = luigi.IntParameter()
    mode = luigi.Parameter()
    first_xml_output = None

    def output(self):

        if self.stage == -1:
            yield self.add_to_output("dataset_sites.txt")
        else:
            # load particles to determine .xml output names
            particles = get_particles()
            myparticles = fei.core.get_stages_from_particles(particles)
            for p in myparticles[self.stage]:
                for channel in p.channels:
                    if not self.first_xml_output:
                        self.first_xml_output = f"{channel.label}.xml"
                    yield self.add_to_output(f"{channel.label}.xml")

    def requires(self):

        # need merged mcParticlesCount.root from stage -1
        yield MergeOutputsTask(
            mode="Merging",
            stage=-1,
            ncpus=luigi.get_setting("local_cpus"),
        )

        # need merged training_input.root from current stage
        if self.stage > -1:

            yield MergeOutputsTask(
                mode="Merging",
                stage=self.stage,
                ncpus=luigi.get_setting("local_cpus"),
            )

        # need .xml training files from all previous stages of FEI training,
        # beginning with stage 1
        if self.stage > 0:

            for fei_stage in range(self.stage):

                yield FEITrainingTask(
                    mode="Training",
                    stage=fei_stage,
                )

    def run(self):

        if self.stage == -1:

            input_ds = luigi.get_setting("gbasf2_input_dslist")
            input_dslist = []
            if input_ds.endswith('.txt'):
                input_dslist = [line.strip() for line in open(input_ds, 'r').readlines()]
            else:
                input_dslist = [input_ds]

            proc_stdouts = []
            for index, ds in enumerate(input_dslist):
                proc = run_with_gbasf2(shlex.split(f"gb2_ds_list {ds} -lg"), capture_output=True)
                proc_stdouts.append(proc.stdout.splitlines())

            sites = []
            for stdout in proc_stdouts:
                sites += [siteline.split(':')[0].replace('DATA', 'TMP') for siteline in stdout if 'SE' in siteline]
            sites = list(set(sites))
            with open(f"{self.get_output_file_name('dataset_sites.txt')}", 'w') as output_sites:
                output_sites.write('\n'.join(sites))
                output_sites.close()
        else:

            # determine directory of outputs:
            outputdir = os.path.dirname(self.get_output_file_name(self.first_xml_output))

            # create symlinks to files, which are needed for current FEI analysis stage
            for key in self.get_input_file_names():
                if key == "mcParticlesCount.root" or key == "training_input.root" or key.endswith(".xml"):
                    filepath = '"' + self.get_input_file_names(key)[0] + '"'
                    adjusted_key = '"' + key + '"'
                    os.system(f"ln -sf {filepath} {adjusted_key}")

            # load path to perform training
            monitor = True if self.stage == 6 else False
            os.system("rm -f Summary.pickle*")
            if not os.path.exists('Summary.pickle'):
                create_fei_path(filelist=[], cache=0, monitor=monitor)
            particles, configuration = pickle.load(open('Summary.pickle', 'rb'))
            fei.do_trainings(particles, configuration)

            # remove symlinks and not needed Summary.pickle files
            for key in self.get_input_file_names():
                if key == "mcParticlesCount.root" or key == "training_input.root" or key.endswith(".xml"):
                    adjusted_key = '"' + key + '"'
                    os.system(f"rm {adjusted_key}")
            os.system("rm -f Summary.pickle*")

            # move *.xml and *.log files to output directory
            if len(glob.glob("*.xml")) > 0:
                os.system(f'mv *.xml {outputdir}')
            if len(glob.glob("*.log")) > 0:
                os.system(f'mv *.log {outputdir}')


class PrepareInputsTask(luigi.Task):

    remote_tmp_directory = luigi.Parameter(significant=False)  # should be set via settings
    remote_initial_se = luigi.Parameter(significant=False)  # should be set via settings

    stage = luigi.IntParameter()
    mode = luigi.Parameter()

    def output(self):

        yield self.add_to_output('fei_analysis_inputs.tar.gz')
        yield self.add_to_output('successfull_input_upload.txt')

    def requires(self):

        # need merged mcParticlesCount.root from stage -1
        yield MergeOutputsTask(
            mode="Merging",
            stage=-1,
            ncpus=luigi.get_setting("local_cpus"),
        )

        # need dataset_sites.txt file from stage -1 of FEI training,
        # which is (ab)used to create that site list
        yield FEITrainingTask(
            mode="Training",
            stage=-1,
        )

        # need .xml training files from current stage of FEI training
        if self.stage > -1:
            yield FEITrainingTask(
                mode="Training",
                stage=self.stage,
            )

        # need .xml training files from all previous stages of FEI training,
        # beginning with stage 1
        if self.stage > 0:

            for fei_stage in range(self.stage):

                yield FEITrainingTask(
                    mode="Training",
                    stage=fei_stage,
                )

    def run(self):

        # create tarball with all required input files for FEIAnalysisTask
        outputs = [outs[0] for outs in self.get_input_file_names().values()
                   if outs[0].endswith('.root') or outs[0].endswith('.xml')]
        taroutname = self.get_output_file_name("fei_analysis_inputs.tar.gz")
        taroutdir = os.path.dirname(taroutname)
        outputsstring = ' '.join(['"'+o+'"' for o in outputs])
        tarcmd = f"rm -f {self.get_output_file_name('successfull_input_upload.txt')}; "
        tarcmd += f"cp {outputsstring} {taroutdir}; "
        tarcmd += f"pushd {taroutdir}; "
        baseoutputsstring = ' '.join(['"'+os.path.basename(o)+'"' for o in outputs])
        tarcmd += f"tar -vczf {taroutname} {baseoutputsstring}; "
        tarcmd += f"rm -f {baseoutputsstring}; popd"
        os.system(tarcmd)

        # upload tarball to initial storage element
        timestamp = datetime.datetime.now().strftime("_%b-%d-%Y_%H-%M-%S")
        foldername = os.path.join(self.remote_tmp_directory.rstrip('/')+timestamp, "stage"+str(self.stage))
        completed_copy = run_with_gbasf2(shlex.split(f"gb2_ds_put -d {self.remote_initial_se} "
                                         f"-i {taroutdir} --datablock sub00 {foldername}"))

        # replicate tarball to other storage element sites (defined by used input datasets)
        dataset_sites = [site.strip() for site in open(self.get_input_file_names('dataset_sites.txt')[0], 'r').readlines()]
        completed_replicas = []
        for ds_site in dataset_sites:
            completed_replicas.append(run_with_gbasf2(shlex.split(f"gb2_ds_rep {foldername}/"
                                      "sub00 -d {ds_site} -s {self.remote_initial_se} --force")))

        if sum([proc.returncode for proc in completed_replicas + [completed_copy]]) == 0:
            with open(f"{self.get_output_file_name('successfull_input_upload.txt')}", "w") as timestampfile:
                timestampfile.write(timestamp)
                timestampfile.close()


class ProduceStatisticsTask(luigi.WrapperTask):

    def requires(self):

        # yield FEITrainingTask(
        #     mode="Training",
        #     stage=0,
        # )

        # yield MergeOutputsTask(
        #     mode="Merging",
        #     stage=0,
        #     ncpus=luigi.get_setting("local_cpus"),
        # )

        # yield PrepareInputsTask(
        #     mode="AnalysisInput",
        #     stage=0,
        #     remote_tmp_directory=luigi.get_setting("remote_tmp_directory"),
        #     remote_initial_se=luigi.get_setting("remote_initial_se"),
        # )

        yield FEIAnalysisTask(
            cache=0,
            monitor=True,
            mode="TrainingInput",
            stage=6,
            gbasf2_project_name_prefix=luigi.get_setting("gbasf2_project_name_prefix"),
            gbasf2_input_dslist=luigi.get_setting("gbasf2_input_dslist"),
        )


if __name__ == '__main__':
    main_task_instance = ProduceStatisticsTask()
    n_gbasf2_tasks = len(list(main_task_instance.requires()))
    luigi.process(main_task_instance, workers=n_gbasf2_tasks)
