"""Fitting a client-chosen name into a bounded column, derived from the model.

Everything about an upload except its bytes is chosen by whoever uploads it:
the filename, its extension, the ``Content-Type`` header. None of them has a
length anybody promised, and all of them land in ``CharField``s — and the
stored path a ``FileField`` writes is built out of the filename too.

Postgres does not truncate. It raises ``StringDataRightTruncation``, the
transaction rolls back, and the upload endpoint answers 500 — to a person
whose only mistake was naming a file. Django's ``max_length`` is a form-layer
and ``full_clean()`` concern; ``.objects.create()`` never checks it, so a
bound declared on a column is not a bound enforced on the path that writes it.

The limits are read off the fields (:func:`max_length_of`) and never typed
here a second time, because a limit typed twice makes the next ``max_length``
change a silent data-loss bug: the column grows and the truncation does not,
or the column shrinks and the truncation does not.

Three functions, and the distinction between them is the point:

* :func:`fit` — prose, or any value whose tail carries no meaning. Cut, with
  the cut MARKED, so a reader can tell "this is the whole value" from "this is
  the front of a longer one". A silent truncation is a lie about the data.
* :func:`fit_filename` — a filename, where the TAIL is the part that carries
  meaning. ``holiday-video-from-the-summer-of-2026.mp4`` cut at the head is
  still recognisably a video; cut at the tail it is a file the browser will
  not know how to open and a person will not recognise in a list. So the
  extension is kept, the stem is cut, and the cut is marked inside the stem.
* :func:`fit_stored_name` — the path a ``FileField`` will store. The only
  function that knows both halves of the arithmetic (the prefix the
  ``upload_to`` builds, and the column the result has to fit), which is why
  the ``upload_to`` callables call it rather than doing the sum themselves.

What is deliberately NOT fitted: ``file_hash``. It is a sha256 hex digest,
exactly 64 characters into a 64 column, so fitting it could only ever be a
no-op — and writing the call would suggest to a reader that truncating a hash
is a thing that can legitimately happen. It is not: the hash is the identity
of the file, and a cut one collides with every other file sharing its prefix.
"""
from __future__ import annotations

import os

#: The marker that says "there was more". One character, so the arithmetic
#: below is honest: Django's ``max_length`` and Postgres' ``varchar(n)`` both
#: count CHARACTERS, so a one-character marker costs exactly one character of
#: budget whatever it encodes to in bytes.
ELLIPSIS = "…"


def max_length_of(model, field_name: str):
    """The declared ``max_length`` of *field_name*, or ``None`` if unbounded."""
    return model._meta.get_field(field_name).max_length


def fit(model, field_name: str, value) -> str:
    """*value* as a string that fits *field_name*, with any cut marked.

    ``None`` becomes ``""``. A field with no ``max_length`` (a ``TextField``)
    is returned whole, so this is safe to call across a model without knowing
    which of its fields are bounded.
    """
    text = "" if value is None else str(value)
    limit = max_length_of(model, field_name)
    if limit is None or len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + ELLIPSIS


def fit_filename(model, field_name: str, value) -> str:
    """A filename that fits *field_name*, keeping the extension.

    The extension is what a browser dispatches on and what a person recognises
    in a list, so it survives; the stem is cut and the cut is marked. An
    extension that is ABSURD on its own — longer than the budget, because a
    filename is a client-chosen string and ``x.`` + 300 characters is a legal
    one — is itself fitted rather than allowed to push the stem out entirely.
    """
    text = "" if value is None else str(value)
    limit = max_length_of(model, field_name)
    if limit is None or len(text) <= limit:
        return text

    stem, extension = os.path.splitext(text)
    # Leave at least a marked single character of stem; otherwise the
    # extension is not an extension, it is the whole name.
    if len(extension) > limit - 2:
        return fit(model, field_name, text)
    keep = limit - len(extension) - 1
    return stem[:keep] + ELLIPSIS + extension


def fit_stored_name(prefix: str, filename: str, model, field_name: str) -> str:
    """``prefix + basename`` trimmed so the whole stored path fits the column.

    This is the arithmetic nobody does by hand correctly. ``Video.original``
    was ``FileField(max_length=100)`` and its ``upload_to`` built
    ``video/<64-char sha256>/<filename>`` — 71 characters before the filename
    is even considered, so any basename over 29 characters overflowed the
    column. Not an edge case: ``holiday-video-from-summer.mp4`` is 29.

    The prefix is never cut — it is the addressing scheme, and a truncated
    hash directory would put two different files in one place.
    """
    limit = max_length_of(model, field_name)
    base = os.path.basename(filename or "")
    if limit is None:
        return prefix + base
    budget = limit - len(prefix)
    if budget <= 0:
        # The prefix alone does not fit: the column is mis-declared and no
        # amount of trimming the name can rescue it. Say so here rather than
        # letting Postgres say it as a 500 on somebody's upload.
        raise ValueError(
            f"{model.__name__}.{field_name} is max_length={limit}, shorter than "
            f"the {len(prefix)}-character prefix its upload_to builds — widen "
            "the column; a filename cannot be trimmed far enough to help"
        )
    if len(base) <= budget:
        return prefix + base
    stem, extension = os.path.splitext(base)
    if len(extension) > budget - 2:
        return prefix + base[: budget - 1] + ELLIPSIS
    keep = budget - len(extension) - 1
    return prefix + stem[:keep] + ELLIPSIS + extension


__all__ = ["ELLIPSIS", "max_length_of", "fit", "fit_filename", "fit_stored_name"]
