# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## UNRELEASED


## v1.0.0
### Changed
* BREAKING CHANGE: instead of specifying a `path` argument for external
  modules, simply provide the name of any importable module.

## v0.5.0
### Changed
* add requirement that fusion2 regression uses the non-interpolated values
* made it such that multitemporal is OK with different sized data stacks, may
  be needed in some cases that use shifttime module.

### Added
* custom exception for when the MT config is invalid.
* progress reporting every 10% of the way through the job

### Fixed
* days of green formatting
* removed hardcoded 'wcc'
* divide by zero in simplelinearregression.pyx

## v0.4.1
### Fixed
+ setup.py needed proper numpy include dirs.

## v0.4.0
### Added
- added CHANGELOG.md

### Changed
- modified module loading to allow import of arbitrary modules that follow
  multitemporal framework protocols

### Fixed
- fixed versioning with file and consistent imports in package and setup.py
