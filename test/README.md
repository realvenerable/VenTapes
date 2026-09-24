# Test and debug scripts

This directory is inherited from Mixtapes and is mostly made up of small,
ad-hoc scripts for inspecting `ytmusicapi` output and reproducing backend
issues. They are retained for learning and code tracing, not as a reliable
automated test suite.

Some scripts may use the network, real credentials, or local caches. The auth
helpers use the VenTapes XDG data path rather than implicitly reading a
repository-local credential file. Read a script before running it, and do not
treat it as an isolated unit test unless it clearly says that it is one.
