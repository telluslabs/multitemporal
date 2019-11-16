#!/usr/bin/env python3

from __future__ import print_function
from __future__ import division
from builtins import str
from builtins import zip
from builtins import range
from past.utils import old_div
import argparse
from copy import deepcopy
import datetime
from functools import partial
import importlib
import json
from multiprocessing import Pool
import os
import re
import sys

import numpy as np
from osgeo import gdal
gdal.UseExceptions()

import sharedmem


class MTConfigError(Exception):
    pass

# output will be a dict of shared memory arrays
OUTPUT = {}


def reglob(path, regexp):
    """ return paths in a directory matching a pattern """
    patt = re.compile(regexp)
    paths = [os.path.join(path,f) for f in os.listdir(path) if patt.search(f)]
    return paths

def write_raster(outfile, data, proj, geo, missing):
    gdal.GDT_UInt8 = gdal.GDT_Byte
    np_dtype = str(data.dtype)
    dtype = eval('gdal.GDT_' + np_dtype.title().replace('Ui','UI'))
    driver = gdal.GetDriverByName('GTiff')
    nb, ny, nx = data.shape
    tfh = driver.Create(outfile, nx, ny, nb, dtype, [])
    tfh.SetProjection(proj)
    tfh.SetGeoTransform(geo)
    for i in range(nb):
        tband = tfh.GetRasterBand(i+1)
        tband.SetNoDataValue(missing)
        tband.WriteArray(data[i,:,:].squeeze())
    del tfh


def find_band(fp, name):
    max_bands = fp.RasterCount
    for i in range(1, max_bands+1):
        b = fp.GetRasterBand(i)
        if name == b.GetDescription():
            return b
    raise Exception("No such band")


def worker(shared, job):

    iblk, istart, iend, blkhgt = job
    sources, steps, blkrow, width, missing_out, nfr = shared

    nb = len(sources)
    npx = width*blkhgt

    for ib, source in enumerate(sources):
        if ib == 0:
            nt = len(source['paths'])
            nyr = old_div(nt,nfr)
            data = missing_out + np.zeros((nb, nfr, nyr, npx), dtype='float32')

        else:
            if nt != len(source['paths']):
                print(('MT worker:\n\t'
                       'nt: {}\n\t'
                       'source["name"]: {}\n\t'
                       'source["regexp"]: {}\n\t'
                       'source dir: {}\n\t'
                       'len(source["paths"]: {}\n\t'
                       'len(non-empty-src-paths): {}\n')
                      .format(nt, source['name'], source['regexp'],
                              set((os.path.basename(sp)
                                   for sp in source['paths'])),
                              len(source['paths']),
                              len([s for s in source['paths'] if s != ''])))

        for ipath, path in enumerate(source['paths']):
            if path == "":
                continue
            fp = gdal.Open(path)
            try:
                if 'bandname' in source:
                    band = find_band(fp, source['bandname'])
                else:
                    band = fp.GetRasterBand(source['bandnum'])
            except:
                continue
            values = band.ReadAsArray(0, iblk*blkrow, width, blkhgt).flatten()
            values = values.astype('float32')
            wgood = np.where(values != source['missing_in'])
            if len(wgood[0]) == 0:
                continue
            iyr = old_div(ipath, nfr)
            ifr = ipath % nfr
            data[ib, ifr, iyr, wgood] = \
                source['offset'] + source['scale']*values[wgood]
            del fp

    sourcenames = [source['name'] for source in sources]
    results = {}

    for step in steps:

        if step['initial'] == True:
            try:
                bix = [sourcenames.index(si) for si in step['inputs']]
            except ValueError as ve:
                raise MTConfigError(
                    'step {} might be mixing sources and outputs'
                    .format(step['name']))
            d = data[bix,:,:,:]
        else:
            d = np.array([results[si] for si in step['inputs']])

        if d.shape[0] == 1:
            d = d.reshape(d.shape[1], d.shape[2], npx)

        results[step['name']] = step['function'](d, missing_out, step['params'])
        if step.get('output', False):
            try:
                OUTPUT[step['name']][:, :, istart:iend] = results[step['name']]
            except Exception as e:
                print('Exception in step "{}"'.format(step['name']))
                raise e
    return str(job) + str(shared)


def run(projdir, outdir, projname, sources, steps,
        blkrow=10, compthresh=0.1, nproc=1, missing_out=-32768.,
        dperframe=1, ymd=False,
        **kwargs):
    """Run the processing pipeline defined by `steps`.

    :param projdir:  Where to search for source data
    :param outdir:  Where to put the outputs
    :param projname:  Prefix for output files
    :param sources:  Product types such as 'ndvi', likely from gips outputs
    :param steps:  Defines the processing pipeline; a series of modules that
        must implement a standard interface (TODO document this interface).
    :param blkrow:  dunno lol
    :param compthresh:  sorta a minimum required ratio of good data to total area
    :param nproc:  Number of worker processes.
    :param missing_out:  no-data value to be used in outputs probably.
    :param dperframe: dunno lol
    :param ymd: use <year><month><day> if ymd else <year><doy> (why not just pass in the format?)
    :param kwargs: unused
    :return: None; see if it worked by try-catching the call
    """
    global OUTPUT

    # set up by finding sources, organizing them, and configuring the run
    for k, source in enumerate(sources):
        print("source: {}".format(source['name']))
        source_paths = reglob(projdir, source['regexp'])

        if len(source_paths) == 0:
            print("there are no data paths for %s" % projdir)
            return

        date_to_source_path = {}
        years = set()
        doys = set()
        initialized = False

        for i, source_path in enumerate(source_paths):
            source_bn = os.path.basename(source_path)

            groups =  re.findall(source['regexp'], source_bn)

            datestr = groups[0][0]
            tilestr = groups[0][1]

            if not ymd:
                # default: YYYYDDD
                date = datetime.datetime.strptime(datestr, '%Y%j')
            else:
                # optional: YYYYMMDD
                date = datetime.datetime.strptime(datestr, '%Y%m%d')

            year = date.year
            doy = int(date.strftime('%j'))
            years.add(year)
            doys.add(doy)
            date_to_source_path[(year, doy)] = source_path

            if not initialized:
                fp = gdal.Open(source_path)
                try:
                    if 'bandname' in source:
                        band = find_band(fp, source['bandname'])
                    else:
                        band = fp.GetRasterBand(source['bandnum'])
                except:
                    continue
                proj = fp.GetProjection()
                geo = fp.GetGeoTransform()
                width = band.XSize
                height = band.YSize
                if 'scale' not in source:
                    source['scale'] = band.GetScale() or 1.
                if 'offset' not in source:
                    source['offset'] = band.GetOffset() or 0.
                if 'missing_in' not in source:
                    source['missing_in'] = band.GetNoDataValue()
                if source['missing_in'] is None:
                    raise Exception("There is no missing value")
                if k == 0:
                    proj_check = proj
                    geo_check = geo
                    width_check = width
                    height_check = height
                else:
                    # after the first source path establishes projection etc, confirm these values
                    # all match for the rest of the source paths
                    GEO_TOLER = 0.0001
                    if proj_check != proj or width_check != width or height_check != height \
                       or (np.array([x[1]-x[0] for x in zip(geo, geo_check)]) > GEO_TOLER).any():
                        raise Exception("Export contents do not match in size, projection,"\
                            "or geospatial properties")

                initialized = True

        firstyr = min(years)
        lastyr = max(years)
        nyr = lastyr - firstyr + 1
        if k == 0:
            firstyr_check = firstyr
            lastyr_check = lastyr
        else:
            if firstyr_check != firstyr or lastyr_check != lastyr:
                emsg = ("Export year ranges do not match: {}!={} or {}!={}"
                        .format(firstyr_check, firstyr, lastyr_check, lastyr))
                print('Nota bene:\n\t' + emsg + '\n   This may be OK')

        doys = np.arange(old_div(366,dperframe)).astype('int') + 1
        nfr = len(doys)

        selpaths = []
        ncomplete = 0
        ntotal = 0

        for year in range(firstyr, lastyr+1):
            for doy in doys:
                try:
                    selpaths.append(date_to_source_path[(year, doy)])
                    ncomplete += 1
                except Exception as e:
                    selpaths.append('')
                ntotal += 1

        source['paths'] = selpaths
        pctcomplete = float(ncomplete)/ntotal
        print("number of paths", len(selpaths))
        print("ncomplete, ntotal, pctcomplete, firstyr, lastyr",\
            ncomplete, ntotal, pctcomplete, firstyr, lastyr)
        if pctcomplete < compthresh:
            msg = ("not enough valid data ({} < {}) percent, for this source"
                   .format(pctcomplete, compthresh))
            raise Exception(msg, source)

    # process the steps

    for step in steps:
        # make sure every step has a name
        # TODO: check for uniqeness
        step['name'] = step.get('name', str(step['module']))

    for step in steps:

        # get functions and parameters for each step by trying to import the
        # steps in different ways until something works.
        # TODO however the implementation sorta 'stutters' with unneeded extra steps
        try:
            # TODO: This reserves the name of multitemporal builtin modules.
            #       Perhaps some sort of module renaming for builtin modules
            #       should occur here?
            mod = importlib.import_module('multitemporal.bin.' + step['module'])
            step['function'] = eval("mod." + step['module'])
        except ImportError:
            mod = importlib.import_module(step['module'])
        try:
            mod = importlib.import_module(step['module'])
            step['function'] = eval("mod." + step['module'].split('.')[-1])
        except ImportError:
            mod = importlib.import_module('multitemporal.bin.' + step['module'])
            step['function'] = eval("mod." + step['module'])
        # do we need to convert the params to float? the json retains data type
        # step['params'] = np.array(step['params']).astype('float32')
        step['initial'] = False

        # determine the number of inputs to this step
        if not isinstance(step['inputs'], list):
            step['inputs'] = [step['inputs']]
        for thisinput in step['inputs']:
            # if this input is in the sources then we know it's a starting point for the pipeline:
            if thisinput in [source['name'] for source in sources]:
                thisnin = nfr
                thisnyrin = nyr
                step['initial'] = True
            else:
                # handle intermediary steps in the pipeline; only one parent is allowed
                parentsteps = [s for s in steps if s['name']==thisinput]
                if len(parentsteps) != 1:
                    raise MTConfigError(
                        'specified input: {}\n\tpossible inputs: {}'
                        .format(thisinput, [s['name'] for s in steps]))
                thisnin = parentsteps[0]['nout']
                thisnyrin = parentsteps[0]['nyrout']

            if 'nin' in step:
                assert step['nin'] == thisnin, "Number of inputs do not match"
            else:
                step['nin'] = thisnin

            if 'nyrin' in step:
                assert step['nyrin'] == thisnyrin, "Number of years do not match"
            else:
                step['nyrin'] = thisnyrin

        # set the number of outputs for each step
        # TODO: any unexpected behavior here?
        try:
            step['nout'] = int(mod.get_nout(step['nin'], step['params']))
        except:
            step['nout'] = step['nin']

        try:
            step['nyrout'] = int(mod.get_nyrout(step['nyrin'], step['params']))
        except:
            step['nyrout'] = step['nyrin']

        if step.get('output', False):
            print("output", mod, (step['nout'], step['nyrout'], height*width))
            OUTPUT[step['name']] = sharedmem.empty(
                (step['nout'], step['nyrout'], height*width), dtype='f4')
            OUTPUT[step['name']][...] = missing_out

    nblocks = old_div(height, blkrow)
    if height % blkrow == 0:
        lastblkrow = blkrow
    else:
        nblocks = nblocks + 1
        lastblkrow = height % blkrow

    # make something to hold selpaths and missing_in for each source
    shared = (sources, steps, blkrow, width, missing_out, nfr)

    # create and run the jobs
    jobs = []
    for iblk in range(nblocks):
        istart = iblk * blkrow * width
        if iblk == nblocks - 1:
            blkhgt = lastblkrow
        else:
            blkhgt = blkrow
        iend = istart + width*blkhgt
        jobs.append((iblk, istart, iend, blkhgt))
    func = partial(worker, shared)
    if nproc > 1:
        results = []
        num_tasks = len(jobs) # == nblocks every time
        pool = Pool(processes=nproc)
        prog=0
        for i, r in enumerate(pool.imap(func, jobs, 1)):
            pct = float(i) / num_tasks * 100
            if pct // 10 > prog:
                prog += 1
                print('mt {:0.02f} complete.\r'.format(pct))
            results.append(r)
    elif nproc == 1:
        results = []
        for job in jobs:
            results.append(func(job))
    elif nproc < 0:
        # secret way to test
        results = [func(jobs[-nproc-1])]
    else:
        raise Exception("nproc can't be zero")

    # write outputs
    if not os.path.exists(outdir):
        os.makedirs(outdir)
    for s in steps:
        if s.get('output', False):
            nout = s['nout']
            nyrout = s['nyrout']
            OUTPUT[s['name']] = OUTPUT[s['name']].reshape(
                nout, nyrout, height, width)
            for i in range(nyrout):
                # all this to just make the file name
                items = [projname, s['name']]
                if nyrout > 1:
                    items.append(str(firstyr + i))
                prefix = "_".join(items)
                outname = prefix + ".tif"
                outpath = os.path.join(outdir, outname)
                outtype = s.get('output_type', OUTPUT[s['name']].dtype)
                print(("writing:", outpath, OUTPUT[s['name']].shape,
                       nout, height, width, 'as {}'.format(outtype)))
                write_raster(
                    outpath,
                    OUTPUT[s['name']][:, i, :, :].reshape(
                        nout, height, width
                    ).astype(outtype),
                    proj, geo, missing_out)


def run_gipsexport(projdir, outdir, **kwargs):
    # assume a specific directory structure associated with GIPS export
    startdirs = [os.path.join(projdir, d) for d in os.listdir(projdir)]
    for startdir in startdirs:
        thisoutdir = os.path.join(outdir, os.path.split(startdir)[1])
        run(startdir, thisoutdir, **kwargs)


def main():
    """Can be run as a standalone program with eg:

    multitemporal/mt.py --nproc=1 --conf=py3-trial.json --projdir=your-files \
        --outdir=py3-trial-out --projname=tpt-proj

    This expects input ndvi rasters into your-files/other-dir-name-here/ per
    gips convention, if your json file `py3-trial.json` looks like this:

        {
            "compthresh": 0.01,
            "dperframe": 1,
            "sources": [{"name": "ndvi", "regexp": "^(\\d{7})_L.._ndvi-toa.tif$", "bandnum": 1}],
            "steps": [{"module": "passthrough", "params": [], "inputs": ["ndvi"], "output": true}]
        }
    """

    parser = argparse.ArgumentParser(description='MultiTemporal Processor')

    # NOTE: do not use argparse defaults. Will be handled separately below
    # TODO: allow all arguments to be specified on the command line
    # except "conf" which maybe will go away

    # execution parameters
    parser.add_argument('--nproc', type=int,
                        help='Number of processors to use')

    parser.add_argument('--blkrow', type=int,
                        help='Rows per block')

    parser.add_argument('--compthresh', type=float,
                        help='Completeness required')

    parser.add_argument('--dperframe', type=int,
                        help='Days per time step (frame)')

    parser.add_argument('--projdir',
                        help='Directory containing timeseries')

    parser.add_argument('--nongips', action="store_true",
                        help='Projdir is not gips compliant')

    parser.add_argument('--ymd', action="store_true",
                        help='Date string is YYMMDD (not GIPS-compliant)')

    parser.add_argument('--projname',
                        help='Project name to use for output files')

    parser.add_argument('--outdir',
                        help='Directory in which to place outputs')

    # consider getting rid of this and just using stdin
    # otherwise --conf is an argument that is a file that has arguments
    # which could be circular
    parser.add_argument('--conf', help='File containing json configuration')

    args = parser.parse_args()

    # steps dictionary lives in conf
    if args.conf is not None:
        # if --conf was specified, read the file and parse the json
        with open(args.conf) as cf:

            conf = json.load(cf)
    else:
        # get json config from stdin
        conf = json.load(sys.stdin)

    # override json configuration options with those from CLI
    args_dict = dict((k,v) for k,v in vars(args).items() if v is not None)
    conf.update(args_dict)

    # apply defaults
    # done after above so that defaults from one do not overwrite the other
    # ok for now -- some things just don't have defaults
    defaults = {
        'nproc': 1,
        'nongips': False,
        'ymd': False,
        'blkrow': 10,
        'compthresh': 0.0,
        'dperframe': 1,
        'missing_out': -32768.,
    }
    for d in defaults:
        conf[d] = conf.get(d, defaults[d])
    run_func = run
    if not conf['nongips']:
        run_func = run_gipsexport
    try:
        run_func(**conf)
    except Exception as e:
        from pprint import pformat
        import traceback
        print(e)
        print(traceback.format_exc())
        sys.exit(33)
    return sys.exit(0)


if __name__ == "__main__":
    main()
