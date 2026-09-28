# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Clocking export import - accepts every clocking export shape this app
knows about (gateway.py's own HIKVision recordList CSV, a tolerated legacy
CSV variant, and the legacy tab-separated .DAT export) through one
doctype. Which shape a given file is gets sniffed from its own content the
moment it's parsed (see is_attendance.controllers.clocking_parsers'
detect_and_parse/sniff_format) - the person uploading never has to know or
choose in advance, and the result is recorded on this doc's own
detected_format field so it's always visible, not implicit.

This doctype used to be split across "Clocking DAT Import" and "Clocking
CSV Import" (formerly "Clocking HIKVision Import"), which differed only in
which parser parse_file() called - every other part of the pipeline
(Issues tracking, the Draft -> Missing Information -> Pending Import ->
Importing -> Completed/Error lifecycle, create_checkins,
sync_clocking_machines) was already fully shared. Consolidating removes
that duplication permanently: a future new vendor shape is "add one more
parse_*() function to clocking_parsers.py," never "clone this doctype
again."

See is_attendance.controllers.clocking_import's module docstring for the
full narrative on *why* the pipeline is shaped the way it is (background
job, before_submit guard, etc.) - that module is what actually drives
every method below; this file only owns doctype registration and handing
the attached file to the right parser.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils.file_manager import get_file

from is_attendance.controllers import clocking_import
from is_attendance.controllers.clocking_parsers import detect_and_parse


class ClockingImport(Document):
	def validate(self):
		# A real field mirroring the standard `owner`, not a redeclared
		# DocField named "owner" itself (Frappe's own DocType.scrub_field_names()
		# unconditionally rejects that fieldname - see uploaded_by's own
		# field description) - set once, idempotent on every later save so
		# it never drifts from who actually created this document, same
		# as `owner` itself never changes after creation.
		if not self.uploaded_by:
			self.uploaded_by = self.owner or frappe.session.user

		clocking_import.validate_import(self)

	def on_update(self):
		clocking_import.after_save_hook(self)

	def before_submit(self):
		clocking_import.before_submit_guard(self)

	@frappe.whitelist()
	def queue_import(self):
		clocking_import.queue_import(self)

	def on_cancel(self):
		clocking_import.cancel_import(self)

	def parse_file(self) -> list[dict]:
		"""Cached per in-memory Document instance - validate() and
		queue_import() can both run against the same loaded doc within one
		request, and re-reading/re-decoding a multi-thousand-line file
		twice is pure waste since the attached file can't change in
		between."""
		cached = getattr(self, "_parsed_rows_cache", None)
		if cached is not None:
			return cached

		filename, content = get_file(self.file)
		detected_format, rows = detect_and_parse(content, filename)
		self.detected_format = detected_format
		self._parsed_rows_cache = rows
		return rows
