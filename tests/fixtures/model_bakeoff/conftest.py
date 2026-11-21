"""The fixture repository has its own tests; the harness suite must not collect them.

They import `calc` from this directory, which is not on the repository's import
path, and they are inputs to the bake-off rather than tests of this project.
"""

collect_ignore = ["tests"]
