import numba
import hpat


def jit(signature_or_function=None, **options):
    # set nopython by default
    if 'nopython' not in options:
        options['nopython'] = True

    _locals = options.pop('locals', {})
    assert isinstance(_locals, dict)

    # put pivots in locals TODO: generalize numba.jit options
    pivots = options.pop('pivots', {})
    assert isinstance(pivots, dict)
    for var, vals in pivots.items():
        _locals[var + ":pivot"] = vals

    h5_types = options.pop('h5_types', {})
    assert isinstance(h5_types, dict)
    for var, vals in h5_types.items():
        _locals[var + ":h5_types"] = vals

    distributed = set(options.pop('distributed', set()))
    assert isinstance(distributed, (set, list))
    _locals["##distributed"] = distributed

    threaded = set(options.pop('threaded', set()))
    assert isinstance(threaded, (set, list))
    _locals["##threaded"] = threaded

    options['locals'] = _locals

    #options['parallel'] = True
    options['parallel'] = {'comprehension': True,
                           'setitem': False,  # FIXME: support parallel setitem
                           'reduction': True,
                           'numpy': True,
                           'stencil': True,
                           'fusion': True,
                           }

    # Option MPI is boolean and true by default
    # it means MPI transport will be used
    mpi_transport_requested = options.pop('MPI', hpat.config.config_transport_mpi_default)
    if not isinstance(mpi_transport_requested, (int, bool)):
        raise ValueError("Option MPI or HPAT_CONFIG_MPI environment variable should be boolean")

    if mpi_transport_requested:
        hpat.config.config_transport_mpi = True
    else:
        hpat.config.config_transport_mpi = False

    # this is for previous version of pipeline manipulation (numba hpat_req <0.38)
    # from .compiler import add_hpat_stages
    # return numba.jit(signature_or_function, user_pipeline_funcs=[add_hpat_stages], **options)
    return numba.jit(signature_or_function, pipeline_class=hpat.compiler.HPATPipeline, **options)
