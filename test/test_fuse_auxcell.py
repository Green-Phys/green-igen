"""Regression tests for green_igen.df.fuse_auxcell with high angular momentum.

fuse_auxcell used to allocate its per-atom offset table with a hard-coded
angular-momentum dimension of 8, so any auxiliary shell with l >= 8 raised an
IndexError (Green-Phys/green-igen#3).
"""

import types

import numpy
import pytest
from pyscf.pbc import gto

from green_igen import df


def _auxcell(lmax, cart):
    # Two atoms with different shell sets, so the per-atom offsets differ.
    basis = {
        'He': [[0, (1.0, 1.0)], [2, (1.2, 1.0)], [lmax, (1.5, 1.0)]],
        'Ne': [[1, (1.1, 1.0)], [lmax, (1.6, 1.0)]],
    }
    return gto.M(atom='He 0 0 0; Ne 1.2 1.2 1.2', a=numpy.eye(3) * 4.0,
                 basis=basis, cart=cart, verbose=0)


@pytest.mark.parametrize('lmax', [8, 9])
def test_fuse_auxcell_high_l_spherical(lmax):
    auxcell = _auxcell(lmax, cart=False)
    mydf = types.SimpleNamespace(eta=0.2)
    fused_cell, fuse = df.fuse_auxcell(mydf, auxcell)

    naux = auxcell.nao_nr()
    chgcell = df.make_modchg_basis(auxcell, mydf.eta)
    assert fused_cell.nao_nr() == naux + chgcell.nao_nr()

    rng = numpy.random.default_rng(7)
    Lpq = rng.standard_normal((fused_cell.nao_nr(), 3))
    out = fuse(Lpq.copy())

    # Reference: every auxcell shell subtracts the smooth chgcell function of
    # the same atom and angular momentum, once per contracted function.
    chg_loc = chgcell.ao_loc_nr()
    chg_offset = {(chgcell.bas_atom(i), chgcell.bas_angular(i)): chg_loc[i]
                  for i in range(chgcell.nbas)}
    aux_loc = auxcell.ao_loc_nr()
    expected = Lpq[:naux].copy()
    chgLpq = Lpq[naux:]
    for i in range(auxcell.nbas):
        l = auxcell.bas_angular(i)
        p0 = chg_offset[(auxcell.bas_atom(i), l)]
        nd = 2 * l + 1
        for i0 in range(aux_loc[i], aux_loc[i + 1], nd):
            expected[i0:i0 + nd] -= chgLpq[p0:p0 + nd]

    assert out.shape == (naux, 3)
    numpy.testing.assert_allclose(out, expected, rtol=0, atol=1e-14)


@pytest.mark.parametrize('lmax', [8, 9])
def test_fuse_auxcell_high_l_cartesian(lmax):
    auxcell = _auxcell(lmax, cart=True)
    mydf = types.SimpleNamespace(eta=0.2)
    fused_cell, fuse = df.fuse_auxcell(mydf, auxcell)

    rng = numpy.random.default_rng(7)
    Lpq = rng.standard_normal((fused_cell.nao_nr(), 3))
    out = fuse(Lpq)

    assert out.shape == (auxcell.nao_nr(cart=False), 3)
    assert numpy.all(numpy.isfinite(out))
