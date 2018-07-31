import json
import re
import subprocess

from b2luigi.batch.processes import BatchProcess, JobStatus
from b2luigi.core.utils import get_log_files


class LSFProcess(BatchProcess):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._batch_job_id = None

    def get_job_status(self):
        assert self._batch_job_id

        output = subprocess.check_output(["bjobs", "-json", "-o", "stat", self._batch_job_id])
        output = output.decode()
        output = json.loads(output)["RECORDS"][0]

        if "STAT" not in output:
            return JobStatus.aborted

        job_status = output["STAT"]

        if job_status == "DONE":
            return JobStatus.successful
        elif job_status == "EXIT":
            return JobStatus.aborted

        return JobStatus.running

    def start_job(self):
        prefix = ["bsub", "-env all"]

        try:
            prefix += ["-q", self.task.queue]
        except AttributeError:
            pass

        # Automatic requeing?

        stdout_log_file, stderr_log_file = get_log_files(self.task)
        prefix += ["-eo", stderr_log_file, "-oo", stdout_log_file]

        output = subprocess.check_output(prefix + self.task_cmd)
        output = output.decode()

        # Output of the form Job <72065926> is submitted to default queue <s>.
        match = re.search(r"<[0-9]+>", output)
        if not match:
            raise RuntimeError("Batch submission failed with output " + output)

        self._batch_job_id = match.group(0)[1:-1]

    def kill_job(self):
        if not self._batch_job_id:
            return

        subprocess.check_call(["bkill", self._batch_job_id], stdout=subprocess.DEVNULL)