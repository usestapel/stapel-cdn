"""
Custom storage backend for CDN files that doesn't add hash suffixes.
"""
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
        """
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
