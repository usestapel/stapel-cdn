"""
Custom storage backend for CDN files that doesn't add hash suffixes.
"""
import os

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage


class OverwriteStorage(FileSystemStorage):
    """
    Custom storage that overwrites existing files instead of adding hash suffixes.
    This is used for CDN files where we want predictable filenames.
    """

    def get_available_name(self, name, max_length=None):
        """
        Return the given filename. If a file with this name already exists,
        delete it before saving the new one.

        ``max_length`` is HONOURED, and it was not always. Django's own
        ``get_available_name`` trims an over-long path to the column it is
        about to be written to; overriding it to return the name unchanged
        silently dropped that guarantee, so a generated path longer than the
        ``FileField``'s ``max_length`` reached Postgres as-is and came back as
        ``StringDataRightTruncation`` — a 500 on somebody's upload, from a
        filename they chose.

        The trim is DETERMINISTIC — the file root is cut, no random suffix is
        added — because predictable names are the whole reason this subclass
        exists. Two different names that trim to the same string can only
        collide inside one content-addressed directory, and that directory is
        the sha256 of the bytes: same directory plus same name means the same
        file, which is exactly the case this storage overwrites on purpose.

        ``upload_to`` already fits the path (``bounds.fit_stored_name``), so
        in the normal intake this is belt and braces. It is not redundant: a
        host that calls ``.save()`` on the field itself never goes through
        ``upload_to`` at all.
        """
        name = str(name).replace("\\", "/")
        if max_length is not None and len(name) > max_length:
            directory, filename = os.path.split(name)
            root, extension = os.path.splitext(filename)
            budget = max_length - len(directory) - len(os.sep if directory else "")
            if budget - len(extension) < 1:
                raise SuspiciousFileOperation(
                    f"Storage cannot fit {name!r} into max_length={max_length}: "
                    f"its directory alone is {len(directory)} characters. Widen "
                    "the column; trimming the filename cannot rescue this."
                )
            name = os.path.join(directory, root[: budget - len(extension)] + extension)

        # Delete the file if it already exists
        if self.exists(name):
            self.delete(name)
        return name

    def _save(self, name, content):
        """
        Save the file, overwriting if it exists.
        """
        # Delete existing file if present
        if self.exists(name):
            self.delete(name)
        return super()._save(name, content)


# Create a single instance of the custom storage
cdn_storage = OverwriteStorage()
