#!/usr/bin/env python
"""Flows related to process memory."""
from __future__ import absolute_import
from __future__ import division

from __future__ import unicode_literals

import logging
import re
from typing import Union

from grr_response_core.lib.rdfvalues import client as rdf_client
from grr_response_core.lib.rdfvalues import client_fs as rdf_client_fs
from grr_response_core.lib.rdfvalues import memory as rdf_memory
from grr_response_core.lib.rdfvalues import paths as rdf_paths
from grr_response_server import flow_base
from grr_response_server import flow_responses
from grr_response_server import server_stubs
from grr_response_server.flows.general import transfer


class YaraProcessScan(flow_base.FlowBase):
  """Scans process memory using Yara."""

  category = "/Memory/"
  friendly_name = "Yara Process Scan"

  args_type = rdf_memory.YaraProcessScanRequest
  behaviours = flow_base.BEHAVIOUR_BASIC

  def Start(self):
    """The start method."""

    # Catch signature issues early.
    rules = self.args.yara_signature.GetRules()
    if not list(rules):
      raise flow_base.FlowError(
          "No rules found in the signature specification.")

    # Same for regex errors.
    if self.args.process_regex:
      re.compile(self.args.process_regex)

    self.CallClient(
        server_stubs.YaraProcessScan,
        request=self.args,
        next_state="ProcessScanResults")

  def ProcessScanResults(
      self,
      responses):
    """Processes the results of the scan."""
    if not responses.success:
      raise flow_base.FlowError(responses.status)

    pids_to_dump = set()

    for response in responses:
      for match in response.matches:
        self.SendReply(match)
        rules = set([m.rule_name for m in match.match])
        rules_string = ",".join(sorted(rules))
        logging.debug("YaraScan match in pid %d (%s) for rules %s.",
                      match.process.pid, match.process.exe, rules_string)
        if self.args.dump_process_on_match:
          pids_to_dump.add(match.process.pid)

      if self.args.include_errors_in_results:
        for error in response.errors:
          self.SendReply(error)

      if self.args.include_misses_in_results:
        for miss in response.misses:
          self.SendReply(miss)

    if pids_to_dump:
      self.CallFlow(
          DumpProcessMemory.__name__,  # pylint: disable=undefined-variable
          pids=list(pids_to_dump),
          skip_special_regions=self.args.skip_special_regions,
          skip_mapped_files=self.args.skip_mapped_files,
          skip_shared_regions=self.args.skip_shared_regions,
          skip_executable_regions=self.args.skip_executable_regions,
          skip_readonly_regions=self.args.skip_readonly_regions,
          next_state="CheckDumpProcessMemoryResults")

  def CheckDumpProcessMemoryResults(self, responses):
    if not responses.success:
      raise flow_base.FlowError(responses.status)

    for response in responses:
      self.SendReply(response)


def _CanonicalizeLegacyWindowsPathSpec(ps):
  """Canonicalize simple PathSpecs that might be from Windows legacy clients."""
  canonicalized = rdf_paths.PathSpec(ps)
  # Detect a path like C:\\Windows\\System32\\GRR.
  if ps.path[1:3] == ":\\" and "/" not in ps.path:
    # Canonicalize the path to /C:/Windows/System32/GRR.
    canonicalized.path = "/" + "/".join(ps.path.split("\\"))
  return canonicalized


def _MigrateLegacyDumpFilesToMemoryAreas(
    response):
  """Migrates a YPDR from dump_files to memory_regions inplace."""
  for info in response.dumped_processes:
    for dump_file in info.dump_files:
      # filename = "%s_%d_%x_%x.tmp" % (process.name, pid, start, end)
      # process.name can contain underscores. Split exactly 3 _ from the right.
      path_without_ext, _ = dump_file.Basename().rsplit(".", 1)
      _, _, start, end = path_without_ext.rsplit("_", 3)
      start = int(start, 16)
      end = int(end, 16)

      info.memory_regions.Append(
          rdf_memory.ProcessMemoryRegion(
              file=_CanonicalizeLegacyWindowsPathSpec(dump_file),
              start=start,
              size=end - start,
          ))
    # Remove dump_files, since new clients do not set it anymore.
    info.dump_files = None


class DumpProcessMemory(flow_base.FlowBase):
  """Acquires memory for a given list of processes."""

  category = "/Memory/"
  friendly_name = "Process Dump"

  args_type = rdf_memory.YaraProcessDumpArgs
  behaviours = flow_base.BEHAVIOUR_BASIC

  def Start(self):
    # Catch regex errors early.
    if self.args.process_regex:
      re.compile(self.args.process_regex)

    if not (self.args.dump_all_processes or self.args.pids or
            self.args.process_regex):
      raise ValueError("No processes to dump specified.")

    self.CallClient(
        server_stubs.YaraProcessDump,
        request=self.args,
        next_state="ProcessResults")

  def ProcessResults(
      self,
      responses):
    """Processes the results of the dump."""
    if not responses.success:
      raise flow_base.FlowError(responses.status)

    response = responses.First()
    _MigrateLegacyDumpFilesToMemoryAreas(response)

    self.SendReply(response)

    for error in response.errors:
      p = error.process
      self.Log("Error dumping process %s (pid %d): %s" %
               (p.name, p.pid, error.error))

    dump_files_to_get = []
    for dumped_process in response.dumped_processes:
      p = dumped_process.process
      self.Log("Getting %d dump files for process %s (pid %d)." %
               (len(dumped_process.memory_regions), p.name, p.pid))
      for region in dumped_process.memory_regions:
        dump_files_to_get.append(region.file)

    if not dump_files_to_get:
      self.Log("No memory dumped, exiting.")
      return

    self.CallFlow(
        transfer.MultiGetFile.__name__,
        pathspecs=dump_files_to_get,
        file_size=1024 * 1024 * 1024,
        use_external_stores=False,
        next_state="DeleteFiles")

  def DeleteFiles(self,
                  responses):
    if not responses.success:
      raise flow_base.FlowError(responses.status)

    for response in responses:
      self.SendReply(response)

      self.CallClient(
          server_stubs.DeleteGRRTempFiles,
          response.pathspec,
          next_state="LogDeleteFiles")

  def LogDeleteFiles(
      self, responses):
    # Check that the DeleteFiles flow worked.
    if not responses.success:
      raise flow_base.FlowError("Could not delete file: %s" % responses.status)
