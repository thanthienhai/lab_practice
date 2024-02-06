import os
import requests
import threading
import shutil
import urllib.request
import timeit
import time
import math
import base64
import queue
import crcmod


class Downloader:
    class Item:
        """Job queue item class"""

        def __init__(self, chunk_id, chunk_range, was_interrupted=False):
            self.chunk_id = chunk_id  # chunk id to be downloaded
            self.chunk_range = chunk_range  # chunk range to download from server
            self.was_interrupted = was_interrupted  # flag to denote if the job was interrupted due to some error

    def __init__(self, url=None, number_of_threads=1):
        """Constructor of Downloader class
        :param url: URL of file to be downloaded (optional)
        :param number_of_threads: Maximum number of threads (optional)
        """
        self.url = url  # url of a file to be downloaded
        self.number_of_threads = number_of_threads  # maximum number of threads
        self.file_size = self.get_file_size()  # remote file's size
        self.if_byte_range = self.is_byte_range_supported()  # if remote server supports byte range
        self.remote_crc32c = self.get_remote_crc32c()  # remote file's checksum
        self.if_contains_crc32c = True if self.remote_crc32c != -1 or self.remote_crc32c is not None else False  # if remote file has a checksum
        self.downloaded_crc32c = None  # checksum of a downloaded file
        self.range_list = list()  # byte range for each download thread
        self.start_time = None  # start time to calculate overall download time
        self.end_time = None  # end time to calculate overall download time
        self.target_filename = os.path.basename(self.url)  # name of a file to be downloaded
        self.status_refresh_rate = 2  # status will be refreshed after certain time (in seconds)
        self.download_durations = [None] * self.number_of_threads  # total download time for each thread (for benchmarking)
        self.q = queue.Queue(maxsize=0)  # worker threads will pick download job from the queue
        self.append_write = "wb"  # default mode will be write in binary
        self.download_status = list()  # current download job status of each thread (for benchmarking)
        self.current_status = ""  # current overall status

    def get_url(self):
        """Returns URL of a file to be downloaded"""
        return self.url

    def set_url(self, url):
        """Set new URL of a file to be downloaded
        :param url: string
        """
        if not url:
            raise ValueError("URL field is empty")
        if not isinstance(url, str):
            raise TypeError("URL must be of string type")
        self.url = url

    def get_number_of_threads(self):
        """Returns maximum number of threads allowed"""
        return self.number_of_threads

    def set_number_of_threads(self, number_of_threads):
        """Set new maximum number of threads allowed (must be a positive number)
        :param number_of_threads: integer
        """
        if number_of_threads <= 0:
            raise ValueError("Number of maximum threads should be positive")
        if not isinstance(number_of_threads, int):
            raise TypeError("Number of maximum threads should be integer")
        self.number_of_threads = number_of_threads

    def __check_request_redirect(self):
        _headers = {'Accept-Encoding': 'identity'}
        _rh = requests.head(url=self.url, headers=_headers)
        if _rh.is_redirect and _rh.headers.get('Location'):
            _rh = requests.head(url=_rh.headers['Location'], headers=_headers)
        return _rh

    def get_file_size(self):
        """Get remote file size in bytes from url
        :return: integer
        """
        _rh = self.__check_request_redirect()
        self.file_size = _rh.headers.get('content-length', 0)
        return int(self.file_size)

    def is_byte_range_supported(self):
        """Return True if accept-range is supported by the url else False
        :return: boolean
        """
        _rh = self.__check_request_redirect()
        server_byte_response = _rh.headers.get('accept-ranges')
        if not server_byte_response or server_byte_response == "none":
            return False
        else:
            return True

    def is_contains_crc32c(self):
        return self.if_contains_crc32c

    def get_remote_crc32c(self):
        _rh = self.__check_request_redirect()
        server_crc32c_response = _rh.headers.get('x-goog-hash')
        if server_crc32c_response:
            response_split = server_crc32c_response.split(', ')
            for response in response_split:
                if response.startswith("crc32c"):
                    return response[7:]
        return None

    def start_download(self):
        """If byte range is supported by server, perform below steps:
            1. Delete temp folder if exists
            2. Fill queue with number of jobs = number of threads
            3. Start worker threads
            4. Keep checking status until all worker downloads reach 100%
            5. Wait till all download complete
            6. Merge chunks of files into a single file
            7. Delete temp folder

        If byte range is not supported server, download file entirely.
        """
        self.start_time = timeit.default_timer()

        if self.if_byte_range:
            if os.path.isdir("temp"):
                shutil.rmtree("temp")
            os.makedirs("temp")

            self.fill_initial_queue()
            for i in range(self.number_of_threads):
                worker = threading.Thread(target=self.download_chunk)
                worker.setDaemon(True)
                worker.start()
            statuss = []
            while self.get_download_status():
                statuss.append(self.current_status)
                if len(statuss) > 2 and len(set(statuss[-3:])) <= 1:
                    return
                time.sleep(self.status_refresh_rate)

            self.q.join()

            with open(self.target_filename, "ab") as target_file:
                for i in range(self.number_of_threads):
                    with open("temp/part" + str(i), "rb") as chunk_file:
                        target_file.write(chunk_file.read())

        return True

    def fill_initial_queue(self):
        """Fill the queue at the start of downloading"""
        self.build_range()
        for chunk_id, chunk_range in enumerate(self.range_list):
            self.q.put(self.Item(chunk_id, chunk_range, False))

    def download_chunk(self):
        """Get job from queue. Download chunk of a file. Range is extracted from job's chunk_range field"""
        while True:
            item = self.q.get()
            try:
                if item.was_interrupted:
                    time.sleep(1)
                    if os.path.isfile("temp/part" + str(item.chunk_id)):
                        self.append_write = "ab"
                        temp = item.chunk_range.split('-')
                        item.chunk_range = str(int(temp[0]) + os.stat("temp/part" + str(item.chunk_id)).st_size) + '-' + temp[1]
                    else:
                        self.append_write = "wb"

                req = urllib.request.Request(self.get_url())
                req.headers['Range'] = 'bytes={}'.format(item.chunk_range)
                with urllib.request.urlopen(req) as response, open('temp/part' + str(item.chunk_id), self.append_write) as out_file:
                    shutil.copyfileobj(response, out_file)
                self.download_durations[item.chunk_id] = timeit.default_timer()

            except IOError:
                item.was_interrupted = True
                self.q.put(item)

            finally:
                self.q.task_done()

    def download_entire_file(self):
        """If byte range is not supported by server, download entire file"""
        r = requests.get(self.url, stream=True)
        with open(self.target_filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)

    def get_status_header(self):
        """Returns header for the download status"""
        status_header = list()
        for i in range(self.number_of_threads):
            status_header.append("chunk" + str(i + 1))
        return '\t\t'.join(status_header)

    def get_download_status(self):
        """Returns current download status per thread separated by tabs in a string format
        :return: string
        """
        self.download_status.clear()
        for i in range(self.number_of_threads):
            if os.path.isfile("temp/part" + str(i)):
                self.download_status.append(str(round(os.stat("temp/part" + str(i)).st_size / (self.file_size / self.number_of_threads) * 100, 2)) + "%")
            else:
                self.download_status.append("0.00%")
        self.current_status = '\t\t'.join(self.download_status)
        if all(x == "100.0%" for x in self.download_status):
            return False
        else:
            return True

    def display_benchmarks(self):
        """Disply benchmark results"""
        print("\nBenchmarking Results:")
        print("\nTotal time taken for download and integrity check:", round(self.end_time - self.start_time, 2), "seconds.")
        if self.if_byte_range:
            print("\nThread\t\tTime Taken\t\tAverage Download Speed")
            for i in range(self.number_of_threads):
                total_time = self.download_durations[i] - self.start_time
                average_speed = ((self.file_size / self.number_of_threads) / total_time) * (8 / (1024 * 1024))
                print(i + 1, "\t\t", round(total_time, 2), "seconds\t\t", round(average_speed, 2), "mbps")

    def get_downloaded_crc32c(self):
        """Compute and returns crc32c checksum of a downloaded file
        :return: string
        """
        file_bytes = open(self.target_filename, 'rb').read()
        crc32c = crcmod.predefined.Crc('crc-32c')
        crc32c.update(file_bytes)
        crc32c_value = base64.b64encode(crc32c.digest())
        self.downloaded_crc32c = str(crc32c_value, 'utf-8')
        return self.downloaded_crc32c

    def check_integrity(self):
        self.get_downloaded_crc32c()
        return self.remote_crc32c == self.downloaded_crc32c

    def build_range(self):
        """Creates the list of byte-range to be downloaded by each thread.
        Total file size is divided by maximum limit of a thread
        """
        i = 0
        chunk_size = int(math.ceil(int(self.file_size) / int(self.number_of_threads)))
        for _ in range(self.number_of_threads):
            if (i + chunk_size) < self.file_size:
                entry = '%s-%s' % (i, i + chunk_size - 1)
            else:
                entry = '%s-%s' % (i, self.file_size)
            i += chunk_size
            self.range_list.append(entry)

    def get_target_filename(self):
        """Returns the target file name"""
        return self.target_filename

    def get_metadata(self):
        """Returns object metadata information"""
        return {
            "url": self.url,
            "number_of_threads": self.number_of_threads,
            "file_size": self.file_size,
            "if_byte_range": self.if_byte_range,
            "if_contains_crc32c": self.if_contains_crc32c,
            "remote_crc32c": self.remote_crc32c,
            "downloaded_crc32c": self.downloaded_crc32c,
            "range_list": self.range_list
        }


def getopts(argv):
    """Parse arguments passed and add them to dictionary
    :param argv: arguments must be url and threads
    :return: dictionary of arguments
    """
    opts = {}  # Empty dictionary to store key-value pairs.
    while argv:  # While there are arguments left to parse...
        if argv[0][0] == '-':  # Found a "-name value" pair.
            opts[argv[0]] = argv[1]  # Add key and value to the dictionary.
        argv = argv[1:]  # Reduce the argument list by copying it starting from index 1.
    return opts


if __name__ == '__main__':
    obj = Downloader(url="https://ni.dhcn.vn/Hoc-dan-Organ.zip",
                     number_of_threads=3)
    obj.start_download()
    print(obj.get_metadata())
    print(obj.get_remote_crc32c())
    print(obj.get_downloaded_crc32c())

# /home/d39env/bin/python /home/dll/goku/tasks/apps/cms/baobongda/downloader.py
