-- Bind reprints to their exact stored payload without altering legacy jobs.
ALTER TABLE jobs ADD COLUMN print_file_id INTEGER;
ALTER TABLE jobs ADD COLUMN plate_index INTEGER;
ALTER TABLE jobs ADD COLUMN ams_mapping JSON;
ALTER TABLE jobs ADD COLUMN payload_sha256 VARCHAR(64);
