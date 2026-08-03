# Pinned SWMM engine

The application targets official EPA SWMM 5.2.4 source at commit
`7952ca837988b1c32f791812eccc9fd64547e093`. The checked-in manifest records
the exact macOS arm64 binary used to execute the guided fixture; the binary
itself is not treated as a portable release artifact.

Release packaging must build and test a target-specific executable from the
pinned commit, record its SHA-256 and dynamic-library closure, include EPA's
public-domain notice and disclaimer, and sign the complete desktop artifact.
The worker rejects any solver path that is not inside an allowed application or
project root, and every exact result stores the solver hash it actually used.
