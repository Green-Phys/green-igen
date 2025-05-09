#!/usr/bin/env python
# Copyright 2014-2019,2021 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
Density fitting

Divide the 3-center Coulomb integrals to two parts.  Compute the local
part in real space, long range part in reciprocal space.

Note when diffuse functions are used in fitting basis, it is easy to cause
linear dependence (non-positive definite) issue under PBC.

Ref:
J. Chem. Phys. 147, 164119 (2017)
'''

import os

import copy
import ctypes
import warnings
import tempfile
import numpy
import h5py
import scipy.linalg
from pyscf import lib
from pyscf import gto
from pyscf.lib import logger
from pyscf.df import addons
from pyscf.df.outcore import _guess_shell_ranges
#from pyscf.pbc.gto.cell import _estimate_rcut
from pyscf.pbc import tools
from . import outcore
from pyscf.pbc.df import ft_ao
from pyscf.pbc.df import aft
from pyscf.pbc.df import df_jk
from pyscf.pbc.df import df_ao2mo
from pyscf.pbc.df.aft import get_nuc
from pyscf.pbc.df.df_jk import zdotCN
from pyscf.pbc.lib.kpts_helper import (is_zero, gamma_point, member, unique, unique_with_wrap_around,
                                       KPT_DIFF_TOL)
from pyscf.pbc.df.aft import _sub_df_jk_
from pyscf import __config__

LINEAR_DEP_THR = getattr(__config__, 'pbc_df_df_DF_lindep', 1e-9)
CUTOFF = getattr(__config__, 'pbc_df_aft_estimate_eta_cutoff', 1e-12)
ETA_MIN = getattr(__config__, 'pbc_df_aft_estimate_eta_min', 0.2)
LONGRANGE_AFT_TURNOVER_THRESHOLD = 2.5
INTEGRAL_PRECISION = getattr(__config__, 'pbc_gto_cell_Cell_precision', 1e-8)
PRECISION = getattr(__config__, 'pbc_df_aft_estimate_eta_precision', 1e-8)
# cutoff penalty due to lattice summation
LATTICE_SUM_PENALTY = 1e-1

def make_auxmol(mol, auxbasis):
    '''Generate a fake Mole object which uses the density fitting auxbasis as
    the basis sets.  If auxbasis is not specified, the optimized auxiliary fitting
    basis set will be generated according to the rules recorded in
    pyscf.df.addons.DEFAULT_AUXBASIS.  If the optimized auxiliary basis is not
    available (either not specified in DEFAULT_AUXBASIS or the basis set of the
    required elements not defined in the optimized auxiliary basis),
    even-tempered Gaussian basis set will be generated.

    See also the paper JCTC, 13, 554 about generating auxiliary fitting basis.
    '''
    import copy
    pmol = copy.copy(mol)  # just need shallow copy

    if auxbasis is None:
        auxbasis = addons.make_auxbasis(mol)
    elif '+etb' in auxbasis:
        dfbasis = auxbasis[:-4]
        auxbasis = addons.aug_etb_for_dfbasis(mol, dfbasis)
    pmol.basis = auxbasis

    if isinstance(auxbasis, (str, list, tuple)):
        uniq_atoms = set([a[0] for a in mol._atom])
        _basis = dict([(a, auxbasis) for a in uniq_atoms])
    elif 'default' in auxbasis:
        uniq_atoms = set([a[0] for a in mol._atom])
        _basis = dict(((a, auxbasis['default']) for a in uniq_atoms))
        _basis.update(auxbasis)
        del(_basis['default'])
    else:
        _basis = auxbasis
    pmol._basis = pmol.format_basis(_basis)

    pmol._atm, pmol._bas, pmol._env = \
            pmol.make_env(mol._atom, pmol._basis, mol._env[:gto.PTR_ENV_START])
    pmol._built = True
    logger.debug(mol, 'num shells = %d, num cGTOs = %d',
                 pmol.nbas, pmol.nao_nr())
    return pmol

def make_modrho_basis(cell, auxbasis=None, drop_eta=None):
    auxcell = make_auxmol(cell, auxbasis)

# Note libcint library will multiply the norm of the integration over spheric
# part sqrt(4pi) to the basis.
    half_sph_norm = numpy.sqrt(.25/numpy.pi)
    steep_shls = []
    ndrop = 0
    rcut = []
    _env = auxcell._env.copy()
    for ib in range(len(auxcell._bas)):
        l = auxcell.bas_angular(ib)
        np = auxcell.bas_nprim(ib)
        nc = auxcell.bas_nctr(ib)
        es = auxcell.bas_exp(ib)
        ptr = auxcell._bas[ib,gto.PTR_COEFF]
        cs = auxcell._env[ptr:ptr+np*nc].reshape(nc,np).T

        if drop_eta is not None and numpy.any(es < drop_eta):
            cs = cs[es>=drop_eta]
            es = es[es>=drop_eta]
            np, ndrop = len(es), ndrop+np-len(es)

        if np > 0:
            pe = auxcell._bas[ib,gto.PTR_EXP]
            auxcell._bas[ib,gto.NPRIM_OF] = np
            _env[pe:pe+np] = es
# int1 is the multipole value. l*2+2 is due to the radial part integral
# \int (r^l e^{-ar^2} * Y_{lm}) (r^l Y_{lm}) r^2 dr d\Omega
            int1 = gto.gaussian_int(l*2+2, es)
            s = numpy.einsum('pi,p->i', cs, int1)
# The auxiliary basis normalization factor is not a must for density expansion.
# half_sph_norm here to normalize the monopole (charge).  This convention can
# simplify the formulism of \int \bar{\rho}, see function auxbar.
            cs = numpy.einsum('pi,i->pi', cs, half_sph_norm/s)
            _env[ptr:ptr+np*nc] = cs.T.reshape(-1)

            steep_shls.append(ib)

            r = _estimate_rcut(es, l, abs(cs).max(axis=1), cell.precision)
            rcut.append(r.max())

    auxcell._env = _env
    auxcell.rcut = max(rcut)

    auxcell._bas = numpy.asarray(auxcell._bas[steep_shls], order='C')
    logger.info(cell, 'Drop %d primitive fitting functions', ndrop)
    logger.info(cell, 'make aux basis, num shells = %d, num cGTOs = %d',
                auxcell.nbas, auxcell.nao_nr())
    logger.info(cell, 'auxcell.rcut %s', auxcell.rcut)
    return auxcell

def make_modchg_basis(auxcell, smooth_eta):
    # * chgcell defines smooth gaussian functions for each angular momentum for
    #   auxcell. The smooth functions may be used to carry the charge
    chgcell = copy.copy(auxcell)  # smooth model density for coulomb integral to carry charge
    half_sph_norm = .5/numpy.sqrt(numpy.pi)
    chg_bas = []
    chg_env = [smooth_eta]
    ptr_eta = auxcell._env.size
    ptr = ptr_eta + 1
    l_max = auxcell._bas[:,gto.ANG_OF].max()
# gaussian_int(l*2+2) for multipole integral:
# \int (r^l e^{-ar^2} * Y_{lm}) (r^l Y_{lm}) r^2 dr d\Omega
    norms = [half_sph_norm/gto.gaussian_int(l*2+2, smooth_eta)
             for l in range(l_max+1)]
    for ia in range(auxcell.natm):
        for l in set(auxcell._bas[auxcell._bas[:,gto.ATOM_OF]==ia, gto.ANG_OF]):
            chg_bas.append([ia, l, 1, 1, 0, ptr_eta, ptr, 0])
            chg_env.append(norms[l])
            ptr += 1

    chgcell._atm = auxcell._atm
    chgcell._bas = numpy.asarray(chg_bas, dtype=numpy.int32).reshape(-1,gto.BAS_SLOTS)
    chgcell._env = numpy.hstack((auxcell._env, chg_env))
    # _estimate_rcut is based on the integral overlap. It's likely too tight for
    # rcut of the model charge. Using the value of functions at rcut seems enough
    # chgcell.rcut = _estimate_rcut(smooth_eta, l_max, 1., auxcell.precision)
    rcut = 15.
    chgcell.rcut = (numpy.log(4*numpy.pi*rcut**2/auxcell.precision) / smooth_eta)**.5

    logger.debug1(auxcell, 'make compensating basis, num shells = %d, num cGTOs = %d',
                  chgcell.nbas, chgcell.nao_nr())
    logger.debug1(auxcell, 'chgcell.rcut %s', chgcell.rcut)
    return chgcell

# kpti == kptj: s2 symmetry
# kpti == kptj == 0 (gamma point): real
def _make_j3c(ggdf, cell, auxcell, kptij_lst, cderi_file):
    from pyscf.pbc import df as gdf
    GDF.weighted_coulG = gdf.GDF.weighted_coulG
    mydf = GDF(ggdf.cell, ggdf.kpts)
    t1 = (logger.process_clock(), logger.perf_counter())
    log = logger.Logger(mydf.stdout, mydf.verbose)
    max_memory = max(2000, mydf.max_memory-lib.current_memory()[0])
    # In pyscf <= 2.0.1 mesh is initialized in constructor
    # for newer versions mesh has to be properly initialized
    #if mydf.mesh is None:
    #    mydf.eta, mydf.mesh, cutoff = _guess_eta(auxcell, mydf.kpts)
    fused_cell, fuse = fuse_auxcell(mydf, auxcell)


    # The ideal way to hold the temporary integrals is to store them in the
    # cderi_file and overwrite them inplace in the second pass.  The current
    # HDF5 library does not have an efficient way to manage free space in
    # overwriting.  It often leads to the cderi_file ~2 times larger than the
    # necessary size.  For now, dumping the DF integral intermediates to a
    # separated temporary file can avoid this issue.  The DF intermediates may
    # be terribly huge. The temporary file should be placed in the same disk
    # as cderi_file.
    swapfile = tempfile.NamedTemporaryFile(dir=os.path.dirname(cderi_file), delete=False)
    fswap = lib.H5TmpFile(swapfile.name)
    # Unlink swapfile to avoid trash
    swapfile = None

    outcore._aux_e2(cell, fused_cell, fswap, 'int3c2e', aosym='s2',
                    kptij_lst=kptij_lst, dataname='j3c-junk', max_memory=max_memory)
    t1 = log.timer_debug1('3c2e', *t1)

    nao = cell.nao_nr()
    naux = auxcell.nao_nr()
    mesh = mydf.mesh
    Gv, Gvbase, kws = cell.get_Gv_weights(mesh)
    b = cell.reciprocal_vectors()
    gxyz = lib.cartesian_prod([numpy.arange(len(x)) for x in Gvbase])
    ngrids = gxyz.shape[0]

    kptis = kptij_lst[:,0]
    kptjs = kptij_lst[:,1]
    kpt_ji = kptjs - kptis

    uniq_kpts, uniq_index, uniq_inverse = unique_with_wrap_around(cell, kpt_ji) #unique(kpt_ji)

    log.debug('Num uniq kpts %d', len(uniq_kpts))
    log.debug2('uniq_kpts %s', uniq_kpts)
    # j2c ~ (-kpt_ji | kpt_ji)
    # Generally speaking, the int2c2e integrals with lattice sum applied on
    # |j> are not necessary hermitian because int2c2e cannot be made converged
    # with regular lattice sum unless the lattice sum vectors (from
    # cell.get_lattice_Ls) are symmetric. After adding the planewaves
    # contributions and fuse(fuse(j2c)), the output matrix is hermitian.
    j2c = fused_cell.pbc_intor('int2c2e', hermi=0, kpts=uniq_kpts)
    max_memory = max(2000, mydf.max_memory - lib.current_memory()[0])
    blksize = max(2048, int(max_memory*.5e6/16/fused_cell.nao_nr()))
    log.debug2('max_memory %s (MB)  blocksize %s', max_memory, blksize)
    for k, kpt in enumerate(uniq_kpts):
        coulG = mydf.weighted_coulG(kpt, False, mesh)
        for p0, p1 in lib.prange(0, ngrids, blksize):
            aoaux = ft_ao.ft_ao(fused_cell, Gv[p0:p1], None, b, gxyz[p0:p1], Gvbase, kpt).T
            LkR = numpy.asarray(aoaux.real, order='C')
            LkI = numpy.asarray(aoaux.imag, order='C')
            aoaux = None

            if is_zero(kpt):  # kpti == kptj
                j2c_p  = lib.ddot(LkR[naux:]*coulG[p0:p1], LkR.T)
                j2c_p += lib.ddot(LkI[naux:]*coulG[p0:p1], LkI.T)
            else:
                j2cR, j2cI = zdotCN(LkR[naux:]*coulG[p0:p1],
                                    LkI[naux:]*coulG[p0:p1], LkR.T, LkI.T)
                j2c_p = j2cR + j2cI * 1j
            j2c[k][naux:] -= j2c_p
            j2c[k][:naux,naux:] -= j2c_p[:,:naux].conj().T
            j2c_p = LkR = LkI = None
        # Symmetrizing the matrix is not must if the integrals converged.
        # Since symmetry cannot be enforced in the pbc_intor('int2c2e'),
        # the aggregated j2c here may have error in hermitian if the range of
        # lattice sum is not big enough.
        j2c[k] = (j2c[k] + j2c[k].conj().T) * .5
        fswap['j2c/%d'%k] = fuse(fuse(j2c[k]).T).T
    j2c = coulG = None

    def cholesky_decomposed_metric(uniq_kptji_id):
        j2c = numpy.asarray(fswap['j2c/%d'%uniq_kptji_id])
        j2c_negative = None
        try:
            j2c = scipy.linalg.cholesky(j2c, lower=True)
            j2ctag = 'CD'
        except scipy.linalg.LinAlgError:
            #msg =('===================================\n'
            #      'J-metric not positive definite.\n'
            #      'It is likely that mesh is not enough.\n'
            #      '===================================')
            #log.error(msg)
            #raise scipy.linalg.LinAlgError('\n'.join([str(e), msg]))
            w, v = scipy.linalg.eigh(j2c)
            log.debug('DF metric linear dependency for kpt %s', uniq_kptji_id)
            log.debug('cond = %.4g, drop %d bfns',
                      w[-1]/w[0], numpy.count_nonzero(w<mydf.linear_dep_threshold))
            v1 = v[:,w>mydf.linear_dep_threshold].conj().T
            v1 /= numpy.sqrt(w[w>mydf.linear_dep_threshold]).reshape(-1,1)
            j2c = v1
            if cell.dimension == 2 and cell.low_dim_ft_type != 'inf_vacuum':
                idx = numpy.where(w < -mydf.linear_dep_threshold)[0]
                if len(idx) > 0:
                    j2c_negative = (v[:,idx]/numpy.sqrt(-w[idx])).conj().T
            w = v = None
            j2ctag = 'eig'
        return j2c, j2c_negative, j2ctag

    feri = h5py.File(cderi_file, 'w')
    feri['j3c-kptij'] = kptij_lst
    nsegs = len(fswap['j3c-junk/0'])
    def make_kpt(uniq_kptji_id, cholesky_j2c):
        kpt = uniq_kpts[uniq_kptji_id]  # kpt = kptj - kpti
        log.debug1('kpt = %s', kpt)
        adapted_ji_idx = numpy.where(uniq_inverse == uniq_kptji_id)[0]
        adapted_kptjs = kptjs[adapted_ji_idx]
        nkptj = len(adapted_kptjs)
        log.debug1('adapted_ji_idx = %s', adapted_ji_idx)

        j2c, j2c_negative, j2ctag = cholesky_j2c

        shls_slice = (auxcell.nbas, fused_cell.nbas)
        Gaux = ft_ao.ft_ao(fused_cell, Gv, shls_slice, b, gxyz, Gvbase, kpt)
        wcoulG = mydf.weighted_coulG(kpt, False, mesh)
        Gaux *= wcoulG.reshape(-1,1)
        kLR = Gaux.real.copy('C')
        kLI = Gaux.imag.copy('C')
        Gaux = None

        if is_zero(kpt):  # kpti == kptj
            aosym = 's2'
            nao_pair = nao*(nao+1)//2

            if cell.dimension == 3:
                vbar = fuse(auxbar(fused_cell))
                ovlp = cell.pbc_intor('int1e_ovlp', hermi=1, kpts=adapted_kptjs)
                ovlp = [lib.pack_tril(s) for s in ovlp]
        else:
            aosym = 's1'
            nao_pair = nao**2

        mem_now = lib.current_memory()[0]
        log.debug2('memory = %s', mem_now)
        max_memory = max(2000, mydf.max_memory-mem_now)
        # nkptj for 3c-coulomb arrays plus 1 Lpq array
        buflen = min(max(int(max_memory*.38e6/16/naux/(nkptj+1)), 1), nao_pair)
        shranges = _guess_shell_ranges(cell, buflen, aosym)
        buflen = max([x[2] for x in shranges])
        # +1 for a pqkbuf
        if aosym == 's2':
            Gblksize = max(16, int(max_memory*.1e6/16/buflen/(nkptj+1)))
        else:
            Gblksize = max(16, int(max_memory*.2e6/16/buflen/(nkptj+1)))
        Gblksize = min(Gblksize, ngrids, 16384)

        def load(aux_slice):
            col0, col1 = aux_slice
            j3cR = []
            j3cI = []
            for k, idx in enumerate(adapted_ji_idx):
                v = numpy.vstack([fswap['j3c-junk/%d/%d'%(idx,i)][0,col0:col1].T
                                  for i in range(nsegs)])
                # vbar is the interaction between the background charge
                # and the auxiliary basis.  0D, 1D, 2D do not have vbar.
                if is_zero(kpt) and cell.dimension == 3:
                    for i in numpy.where(vbar != 0)[0]:
                        v[i] -= vbar[i] * ovlp[k][col0:col1]
                j3cR.append(numpy.asarray(v.real, order='C'))
                if is_zero(kpt) and gamma_point(adapted_kptjs[k]):
                    j3cI.append(None)
                else:
                    j3cI.append(numpy.asarray(v.imag, order='C'))
                v = None
            return j3cR, j3cI

        pqkRbuf = numpy.empty(buflen*Gblksize)
        pqkIbuf = numpy.empty(buflen*Gblksize)
        # buf for ft_aopair
        buf = numpy.empty(nkptj*buflen*Gblksize, dtype=numpy.complex128)
        cols = [sh_range[2] for sh_range in shranges]
        locs = numpy.append(0, numpy.cumsum(cols))
        tasks = zip(locs[:-1], locs[1:])
        for istep, (j3cR, j3cI) in enumerate(lib.map_with_prefetch(load, tasks)):
            bstart, bend, ncol = shranges[istep]
            log.debug1('int3c2e [%d/%d], AO [%d:%d], ncol = %d',
                       istep+1, len(shranges), bstart, bend, ncol)
            if aosym == 's2':
                shls_slice = (bstart, bend, 0, bend)
            else:
                shls_slice = (bstart, bend, 0, cell.nbas)

            for p0, p1 in lib.prange(0, ngrids, Gblksize):
                dat = ft_ao.ft_aopair_kpts(cell, Gv[p0:p1], shls_slice, aosym,
                                           b, gxyz[p0:p1], Gvbase, kpt,
                                           adapted_kptjs, out=buf)
                nG = p1 - p0
                for k, ji in enumerate(adapted_ji_idx):
                    aoao = dat[k].reshape(nG,ncol)
                    pqkR = numpy.ndarray((ncol,nG), buffer=pqkRbuf)
                    pqkI = numpy.ndarray((ncol,nG), buffer=pqkIbuf)
                    pqkR[:] = aoao.real.T
                    pqkI[:] = aoao.imag.T

                    lib.dot(kLR[p0:p1].T, pqkR.T, -1, j3cR[k][naux:], 1)
                    lib.dot(kLI[p0:p1].T, pqkI.T, -1, j3cR[k][naux:], 1)
                    if not (is_zero(kpt) and gamma_point(adapted_kptjs[k])):
                        lib.dot(kLR[p0:p1].T, pqkI.T, -1, j3cI[k][naux:], 1)
                        lib.dot(kLI[p0:p1].T, pqkR.T,  1, j3cI[k][naux:], 1)

            for k, ji in enumerate(adapted_ji_idx):
                if is_zero(kpt) and gamma_point(adapted_kptjs[k]):
                    v = fuse(j3cR[k])
                else:
                    v = fuse(j3cR[k] + j3cI[k] * 1j)
                if j2ctag == 'CD':
                    v = scipy.linalg.solve_triangular(j2c, v, lower=True, overwrite_b=True)
                    feri['j3c/%d/%d'%(ji,istep)] = v
                else:
                    feri['j3c/%d/%d'%(ji,istep)] = lib.dot(j2c, v)

                # low-dimension systems
                if j2c_negative is not None:
                    feri['j3c-/%d/%d'%(ji,istep)] = lib.dot(j2c_negative, v)
            j3cR = j3cI = None

        #for ji in adapted_ji_idx:
        #    del(fswap['j3c-junk/%d'%ji])

    # Wrapped around boundary and symmetry between k and -k can be used
    # explicitly for the metric integrals.  We consider this symmetry
    # because it is used in the df_ao2mo module when contracting two 3-index
    # integral tensors to the 4-index 2e integral tensor. If the symmetry
    # related k-points are treated separately, the resultant 3-index tensors
    # may have inconsistent dimension due to the numerial noise when handling
    # linear dependency of j2c.
    def conj_j2c(cholesky_j2c):
        j2c, j2c_negative, j2ctag = cholesky_j2c
        if j2c_negative is None:
            return j2c.conj(), None, j2ctag
        else:
            return j2c.conj(), j2c_negative.conj(), j2ctag

    a = cell.lattice_vectors() / (2*numpy.pi)
    def kconserve_indices(kpt):
        '''search which (kpts+kpt) satisfies momentum conservation'''
        kdif = numpy.einsum('wx,ix->wi', a, uniq_kpts + kpt)
        kdif_int = numpy.rint(kdif)
        mask = numpy.einsum('wi->i', abs(kdif - kdif_int)) < KPT_DIFF_TOL
        uniq_kptji_ids = numpy.where(mask)[0]
        return uniq_kptji_ids

    done = numpy.zeros(len(uniq_kpts), dtype=bool)
    for k, kpt in enumerate(uniq_kpts):
        if done[k]:
            continue

        log.debug1('Cholesky decomposition for j2c at kpt %s', k)
        cholesky_j2c = cholesky_decomposed_metric(k)

        # The k-point k' which has (k - k') * a = 2n pi. Metric integrals have the
        # symmetry S = S
        uniq_kptji_ids = kconserve_indices(-kpt)
        log.debug1("Symmetry pattern (k - %s)*a= 2n pi", kpt)
        log.debug1("    make_kpt for uniq_kptji_ids %s", uniq_kptji_ids)
        for uniq_kptji_id in uniq_kptji_ids:
            if not done[uniq_kptji_id]:
                make_kpt(uniq_kptji_id, cholesky_j2c)
        done[uniq_kptji_ids] = True

        # The k-point k' which has (k + k') * a = 2n pi. Metric integrals have the
        # symmetry S = S*
        uniq_kptji_ids = kconserve_indices(kpt)
        log.debug1("Symmetry pattern (k + %s)*a= 2n pi", kpt)
        log.debug1("    make_kpt for %s", uniq_kptji_ids)
        cholesky_j2c = conj_j2c(cholesky_j2c)
        for uniq_kptji_id in uniq_kptji_ids:
            if not done[uniq_kptji_id]:
                make_kpt(uniq_kptji_id, cholesky_j2c)
        done[uniq_kptji_ids] = True

    feri.close()


class GDF(aft.AFTDF):
    '''Gaussian density fitting
    '''
    def __init__(self, cell, kpts=numpy.zeros((1,3))):
        self.cell = cell
        self.stdout = cell.stdout
        self.verbose = cell.verbose
        self.max_memory = cell.max_memory

        self.kpts = kpts  # default is gamma point
        self.kpts_band = None
        self._auxbasis = None

        # Search for optimized eta and mesh here.
        if cell.dimension == 0:
            self.eta = 0.2
            self.mesh = cell.mesh
        else:
            ke_cutoff = tools.mesh_to_cutoff(cell.lattice_vectors(), cell.mesh)
            ke_cutoff = ke_cutoff[:cell.dimension].min()
            eta_cell = estimate_eta_for_ke_cutoff(cell, ke_cutoff, cell.precision)
            eta_guess = estimate_eta(cell, cell.precision)
            logger.debug3(self, 'eta_guess = %g', eta_guess)
            if eta_cell < eta_guess:
                self.eta = eta_cell
                self.mesh = cell.mesh
            else:
                self.eta = eta_guess
                ke_cutoff = estimate_ke_cutoff_for_eta(cell, self.eta, cell.precision)
                self.mesh = cutoff_to_mesh(cell.lattice_vectors(), ke_cutoff)
                if cell.dimension < 2 or cell.low_dim_ft_type == 'inf_vacuum':
                    self.mesh[cell.dimension:] = cell.mesh[cell.dimension:]
        self.mesh = _round_off_to_odd_mesh(self.mesh)

        # exp_to_discard to remove diffused fitting functions. The diffused
        # fitting functions may cause linear dependency in DF metric. Removing
        # the fitting functions whose exponents are smaller than exp_to_discard
        # can improve the linear dependency issue. However, this parameter
        # affects the quality of the auxiliary basis. The default value of
        # this parameter was set to 0.2 in v1.5.1 or older and was changed to
        # 0 since v1.5.2.
        self.exp_to_discard = cell.exp_to_discard

        # The following attributes are not input options.
        self.exxdiv = None  # to mimic KRHF/KUHF object in function get_coulG
        self.auxcell = None
        self.blockdim = getattr(__config__, 'pbc_df_df_DF_blockdim', 240)
        self.linear_dep_threshold = LINEAR_DEP_THR
        self._j_only = False
# If _cderi_to_save is specified, the 3C-integral tensor will be saved in this file.
        self._cderi_to_save = tempfile.NamedTemporaryFile(dir=lib.param.TMPDIR)
# If _cderi is specified, the 3C-integral tensor will be read from this file
        self._cderi = None
        self._rsh_df = {}  # Range separated Coulomb DF objects
        self._keys = set(self.__dict__.keys())

    @property
    def auxbasis(self):
        return self._auxbasis
    @auxbasis.setter
    def auxbasis(self, x):
        if self._auxbasis != x:
            self._auxbasis = x
            self.auxcell = None
            self._cderi = None

    def reset(self, cell=None):
        if cell is not None:
            self.cell = cell
        self.auxcell = None
        self._cderi = None
        self._cderi_to_save = tempfile.NamedTemporaryFile(dir=lib.param.TMPDIR)
        self._rsh_df = {}
        return self

    @property
    def gs(self):
        return [n//2 for n in self.mesh]
    @gs.setter
    def gs(self, x):
        warnings.warn('Attribute .gs is deprecated. It is replaced by attribute .mesh.\n'
                      'mesh = the number of PWs (=2*gs+1) for each direction.')
        self.mesh = [2*n+1 for n in x]

    def dump_flags(self, verbose=None):
        log = logger.new_logger(self, verbose)
        log.info('\n')
        log.info('******** %s ********', self.__class__)
        log.info('mesh = %s (%d PWs)', self.mesh, numpy.prod(self.mesh))
        if self.auxcell is None:
            log.info('auxbasis = %s', self.auxbasis)
        else:
            log.info('auxbasis = %s', self.auxcell.basis)
        log.info('eta = %s', self.eta)
        log.info('exp_to_discard = %s', self.exp_to_discard)
        if isinstance(self._cderi, str):
            log.info('_cderi = %s  where DF integrals are loaded (readonly).',
                     self._cderi)
        elif isinstance(self._cderi_to_save, str):
            log.info('_cderi_to_save = %s', self._cderi_to_save)
        else:
            log.info('_cderi_to_save = %s', self._cderi_to_save.name)
        log.info('len(kpts) = %d', len(self.kpts))
        log.debug1('    kpts = %s', self.kpts)
        if self.kpts_band is not None:
            log.info('len(kpts_band) = %d', len(self.kpts_band))
            log.debug1('    kpts_band = %s', self.kpts_band)
        return self

    def check_sanity(self):
        return lib.StreamObject.check_sanity(self)

    def build(self, j_only=None, with_j3c=True, kpts_band=None):
        if self.kpts_band is not None:
            self.kpts_band = numpy.reshape(self.kpts_band, (-1,3))
        if kpts_band is not None:
            kpts_band = numpy.reshape(kpts_band, (-1,3))
            if self.kpts_band is None:
                self.kpts_band = kpts_band
            else:
                self.kpts_band = unique(numpy.vstack((self.kpts_band,kpts_band)))[0]

        self.check_sanity()
        self.dump_flags()

        self.auxcell = make_modrho_basis(self.cell, self.auxbasis,
                                         self.exp_to_discard)

        # Remove duplicated k-points. Duplicated kpts may lead to a buffer
        # located in incore.wrap_int3c larger than necessary. Integral code
        # only fills necessary part of the buffer, leaving some space in the
        # buffer unfilled.
        uniq_idx = unique(self.kpts)[1]
        kpts = numpy.asarray(self.kpts)[uniq_idx]
        if self.kpts_band is None:
            kband_uniq = numpy.zeros((0,3))
        else:
            kband_uniq = [k for k in self.kpts_band if len(member(k, kpts))==0]
        if j_only is None:
            j_only = self._j_only
        if j_only:
            kall = numpy.vstack([kpts,kband_uniq])
            kptij_lst = numpy.hstack((kall,kall)).reshape(-1,2,3)
        else:
            kptij_lst = [(ki, kpts[j]) for i, ki in enumerate(kpts) for j in range(i+1)]
            kptij_lst.extend([(ki, kj) for ki in kband_uniq for kj in kpts])
            kptij_lst.extend([(ki, ki) for ki in kband_uniq])
            kptij_lst = numpy.asarray(kptij_lst)

        if with_j3c:
            if isinstance(self._cderi_to_save, str):
                cderi = self._cderi_to_save
            else:
                cderi = self._cderi_to_save.name
            if isinstance(self._cderi, str):
                if self._cderi == cderi and os.path.isfile(cderi):
                    logger.warn(self, 'DF integrals in %s (specified by '
                                '._cderi) is overwritten by GDF '
                                'initialization. ', cderi)
                else:
                    logger.warn(self, 'Value of ._cderi is ignored. '
                                'DF integrals will be saved in file %s .',
                                cderi)
            self._cderi = cderi
            t1 = (logger.process_clock(), logger.perf_counter())
            self._make_j3c(self.cell, self.auxcell, kptij_lst, cderi)
            t1 = logger.timer_debug1(self, 'j3c', *t1)
        return self

    _make_j3c = _make_j3c

    def has_kpts(self, kpts):
        if kpts is None:
            return True
        else:
            kpts = numpy.asarray(kpts).reshape(-1,3)
            if self.kpts_band is None:
                return all((len(member(kpt, self.kpts))>0) for kpt in kpts)
            else:
                return all((len(member(kpt, self.kpts))>0 or
                            len(member(kpt, self.kpts_band))>0) for kpt in kpts)

    def auxbar(self, fused_cell=None):
        r'''
        Potential average = \sum_L V_L*Lpq

        The coulomb energy is computed with chargeless density
        \int (rho-C) V,  C = (\int rho) / vol = Tr(gamma,S)/vol
        It is equivalent to removing the averaged potential from the short range V
        vs = vs - (\int V)/vol * S
        '''
        if fused_cell is None:
            fused_cell, fuse = fuse_auxcell(self, self.auxcell)
        aux_loc = fused_cell.ao_loc_nr()
        vbar = numpy.zeros(aux_loc[-1])
        if fused_cell.dimension != 3:
            return vbar

        half_sph_norm = .5/numpy.sqrt(numpy.pi)
        for i in range(fused_cell.nbas):
            l = fused_cell.bas_angular(i)
            if l == 0:
                es = fused_cell.bas_exp(i)
                if es.size == 1:
                    vbar[aux_loc[i]] = -1/es[0]
                else:
                    # Remove the normalization to get the primitive contraction coeffcients
                    norms = half_sph_norm/gto.gaussian_int(2, es)
                    cs = numpy.einsum('i,ij->ij', 1/norms, fused_cell._libcint_ctr_coeff(i))
                    vbar[aux_loc[i]:aux_loc[i+1]] = numpy.einsum('in,i->n', cs, -1/es)
        # TODO: fused_cell.cart and l%2 == 0: # 6d 10f ...
        # Normalization coefficients are different in the same shell for cartesian
        # basis. E.g. the d-type functions, the 5 d-type orbitals are normalized wrt
        # the integral \int r^2 * r^2 e^{-a r^2} dr.  The s-type 3s orbital should be
        # normalized wrt the integral \int r^0 * r^2 e^{-a r^2} dr. The different
        # normalization was not built in the basis.
        vbar *= numpy.pi/fused_cell.vol
        return vbar

    def sr_loop(self, kpti_kptj=numpy.zeros((2,3)), max_memory=2000,
                compact=True, blksize=None):
        '''Short range part'''
        if self._cderi is None:
            self.build()
        cell = self.cell
        kpti, kptj = kpti_kptj
        unpack = is_zero(kpti-kptj) and not compact
        is_real = is_zero(kpti_kptj)
        nao = cell.nao_nr()
        if blksize is None:
            if is_real:
                blksize = max_memory*1e6/8/(nao**2*2)
            else:
                blksize = max_memory*1e6/16/(nao**2*2)
            blksize /= 2  # For prefetch
            blksize = max(16, min(int(blksize), self.blockdim))
            logger.debug3(self, 'max_memory %d MB, blksize %d', max_memory, blksize)

        def load(aux_slice):
            b0, b1 = aux_slice
            if is_real:
                LpqR = numpy.asarray(j3c[b0:b1])
                if unpack:
                    LpqR = lib.unpack_tril(LpqR).reshape(-1,nao**2)
                LpqI = numpy.zeros_like(LpqR)
            else:
                Lpq = numpy.asarray(j3c[b0:b1])
                LpqR = numpy.asarray(Lpq.real, order='C')
                LpqI = numpy.asarray(Lpq.imag, order='C')
                Lpq = None
                if unpack:
                    LpqR = lib.unpack_tril(LpqR).reshape(-1,nao**2)
                    LpqI = lib.unpack_tril(LpqI, lib.ANTIHERMI).reshape(-1,nao**2)
            return LpqR, LpqI

        with _load3c(self._cderi, 'j3c', kpti_kptj, 'j3c-kptij') as j3c:
            slices = lib.prange(0, j3c.shape[0], blksize)
            for LpqR, LpqI in lib.map_with_prefetch(load, slices):
                yield LpqR, LpqI, 1
                LpqR = LpqI = None

        if cell.dimension == 2 and cell.low_dim_ft_type != 'inf_vacuum':
            # Truncated Coulomb operator is not postive definite. Load the
            # CDERI tensor of negative part.
            with _load3c(self._cderi, 'j3c-', kpti_kptj, 'j3c-kptij',
                         ignore_key_error=True) as j3c:
                slices = lib.prange(0, j3c.shape[0], blksize)
                for LpqR, LpqI in lib.map_with_prefetch(load, slices):
                    yield LpqR, LpqI, -1
                    LpqR = LpqI = None

    weighted_coulG = aft.weighted_coulG
    _int_nuc_vloc = aft._int_nuc_vloc
    get_nuc = aft.get_nuc  # noqa: F811
    get_pp = aft.get_pp

    # Note: Special exxdiv by default should not be used for an arbitrary
    # input density matrix. When the df object was used with the molecular
    # post-HF code, get_jk was often called with an incomplete DM (e.g. the
    # core DM in CASCI). An SCF level exxdiv treatment is inadequate for
    # post-HF methods.
    def get_jk(self, dm, hermi=1, kpts=None, kpts_band=None,
               with_j=True, with_k=True, omega=None, exxdiv=None):
        if omega is not None:  # J/K for RSH functionals
            cell = self.cell
            # * AFT is computationally more efficient than GDF if the Coulomb
            #   attenuation tends to the long-range role (i.e. small omega).
            # * Note: changing to AFT integrator may cause small difference to
            #   the GDF integrator. If a very strict GDF result is desired,
            #   we can disable this trick by setting
            #   LONGRANGE_AFT_TURNOVER_THRESHOLD to 0.
            # * The sparse mesh is not appropriate for low dimensional systems
            #   with infinity vacuum since the ERI may require large mesh to
            #   sample density in vacuum.
            if (omega < LONGRANGE_AFT_TURNOVER_THRESHOLD and
                cell.dimension >= 2 and cell.low_dim_ft_type != 'inf_vacuum'):
                mydf = aft.AFTDF(cell, self.kpts)
                mydf.ke_cutoff = aft.estimate_ke_cutoff_for_omega(cell, omega)
                mydf.mesh = tools.cutoff_to_mesh(cell.lattice_vectors(), mydf.ke_cutoff)
            else:
                mydf = self
            return _sub_df_jk_(mydf, dm, hermi, kpts, kpts_band,
                               with_j, with_k, omega, exxdiv)

        if kpts is None:
            if numpy.all(self.kpts == 0):
                # Gamma-point calculation by default
                kpts = numpy.zeros(3)
            else:
                kpts = self.kpts
        kpts = numpy.asarray(kpts)

        if kpts.shape == (3,):
            return df_jk.get_jk(self, dm, hermi, kpts, kpts_band, with_j,
                                with_k, exxdiv)

        vj = vk = None
        if with_k:
            vk = df_jk.get_k_kpts(self, dm, hermi, kpts, kpts_band, exxdiv)
        if with_j:
            vj = df_jk.get_j_kpts(self, dm, hermi, kpts, kpts_band)
        return vj, vk

    get_eri = get_ao_eri = df_ao2mo.get_eri
    ao2mo = get_mo_eri = df_ao2mo.general
    ao2mo_7d = df_ao2mo.ao2mo_7d

    def update_mp(self):
        mf = copy.copy(self)
        mf.with_df = self
        return mf

    def update_cc(self):
        pass

    def update(self):
        pass

################################################################################
# With this function to mimic the molecular DF.loop function, the pbc gamma
# point DF object can be used in the molecular code
    def loop(self, blksize=None):
        cell = self.cell
        if cell.dimension == 2 and cell.low_dim_ft_type != 'inf_vacuum':
            raise RuntimeError('ERIs of PBC-2D systems are not positive '
                               'definite. Current API only supports postive '
                               'definite ERIs.')

        if blksize is None:
            blksize = self.blockdim
        for LpqR, LpqI, sign in self.sr_loop(compact=True, blksize=blksize):
            # LpqI should be 0 for gamma point DF
            # assert(numpy.linalg.norm(LpqI) < 1e-12)
            yield LpqR

    def get_naoaux(self):
        '''The dimension of auxiliary basis at gamma point'''
# determine naoaux with self._cderi, because DF object may be used as CD
# object when self._cderi is provided.
        if self._cderi is None:
            self.build()
        # self._cderi['j3c/k_id/seg_id']
        with addons.load(self._cderi, 'j3c/0') as feri:
            if isinstance(feri, h5py.Group):
                naux = feri['0'].shape[0]
            else:
                naux = feri.shape[0]

        cell = self.cell
        if (cell.dimension == 2 and cell.low_dim_ft_type != 'inf_vacuum' and
            not isinstance(self._cderi, numpy.ndarray)):
            with h5py.File(self._cderi, 'r') as feri:
                if 'j3c-/0' in feri:
                    dat = feri['j3c-/0']
                    if isinstance(dat, h5py.Group):
                        naux += dat['0'].shape[0]
                    else:
                        naux += dat.shape[0]
        return naux

DF = GDF

def _cubic2nonorth_factor(a):
    '''The factors to transform the energy cutoff from cubic lattice to
    non-orthogonal lattice. Energy cutoff is estimated based on cubic lattice.
    It needs to be rescaled for the non-orthogonal lattice to ensure that the
    minimal Gv vector in the reciprocal space is larger than the required
    energy cutoff.
    '''
    # Using ke_cutoff to set up a sphere, the sphere needs to be completely
    # inside the box defined by Gv vectors
    abase = a / numpy.linalg.norm(a, axis=1)[:,None]
    bbase = numpy.linalg.inv(abase.T)
    overlap = numpy.einsum('ix,ix->i', abase, bbase)
    return 1./overlap**2

def cutoff_to_mesh(a, cutoff):
    r'''
    Convert KE cutoff to FFT-mesh

        uses KE = k^2 / 2, where k_max ~ \pi / grid_spacing

    Args:
        a : (3,3) ndarray
            The real-space unit cell lattice vectors. Each row represents a
            lattice vector.
        cutoff : float
            KE energy cutoff in a.u.

    Returns:
        mesh : (3,) array
    '''
    b = 2 * numpy.pi * numpy.linalg.inv(a.T)
    cutoff = cutoff * _cubic2nonorth_factor(a)
    mesh = numpy.ceil(numpy.sqrt(2*cutoff)/lib.norm(b, axis=1) * 2).astype(int)
    return mesh

"""
def estimate_eta_for_ke_cutoff(cell, ke_cutoff, precision=None):
    '''Given ke_cutoff, the upper bound of eta to produce the required
    precision in AFTDF Coulomb integrals.
    '''
    if precision is None:
        precision = cell.precision
    ai = numpy.hstack(cell.bas_exps()).max()
    aij = ai * 2
    ci = gto.gto_norm(0, ai)
    norm_ang = (4*numpy.pi)**-1.5
    c1 = ci**2 * norm_ang
    fac = 64*numpy.pi**5*c1 * (aij*ke_cutoff*2)**-.5 / precision

    eta = 4.
    eta = 1./(numpy.log(fac * eta**-1.5)*2 / ke_cutoff - 1./aij)
    if eta < 0:
        eta = 4.
    else:
        eta = min(4., eta)
    return eta
"""
def estimate_eta_for_ke_cutoff(cell, ke_cutoff, precision=PRECISION):
    '''Given ke_cutoff, the upper bound of eta to produce the required
    precision in AFTDF Coulomb integrals.
    '''
    # search eta for interaction between GTO(eta) and point charge at the same
    # location so that
    # \sum_{k^2/2 > ke_cutoff} weight*4*pi/k^2 GTO(eta, k) < precision
    # GTO(eta, k) = Fourier transform of Gaussian e^{-eta r^2}

    lmax = numpy.max(cell._bas[:,gto.ANG_OF])
    kmax = (ke_cutoff*2)**.5
    # The interaction between two s-type density distributions should be
    # enough for the error estimation.  Put lmax here to increate Ecut for
    # slightly better accuracy
    log_rest = numpy.log(precision / (32*numpy.pi**2 * kmax**(lmax-1)))
    log_eta = -1
    eta = kmax**2/4 / (-log_eta - log_rest)
    return eta

"""
def estimate_ke_cutoff_for_eta(cell, eta, precision=None):
    '''Given eta, the lower bound of ke_cutoff to produce the required
    precision in AFTDF Coulomb integrals.
    '''
    if precision is None:
        precision = cell.precision
    ai = numpy.hstack(cell.bas_exps()).max()
    aij = ai * 2
    ci = gto.gto_norm(0, ai)
    ck = gto.gto_norm(0, eta)
    theta = 1./(1./aij + 1./eta)
    Norm_ang = (4*numpy.pi)**-1.5
    fac = 32*numpy.pi**5 * ci**2*ck*Norm_ang * (2*aij) / (aij*eta)**1.5
    fac /= precision

    Ecut = 20.
    Ecut = numpy.log(fac * (Ecut*2)**(-.5)) * 2*theta
    Ecut = numpy.log(fac * (Ecut*2)**(-.5)) * 2*theta
    return Ecut
"""
def estimate_ke_cutoff_for_eta(cell, eta, precision=PRECISION):
    '''Given eta, the lower bound of ke_cutoff to produce the required
    precision in AFTDF Coulomb integrals.
    '''
    # estimate ke_cutoff for interaction between GTO(eta) and point charge at
    # the same location so that
    # \sum_{k^2/2 > ke_cutoff} weight*4*pi/k^2 GTO(eta, k) < precision
    # \sum_{k^2/2 > ke_cutoff} weight*4*pi/k^2 GTO(eta, k)
    # ~ \int_kmax^infty 4*pi/k^2 GTO(eta,k) dk^3
    # = (4*pi)^2 *2*eta/kmax^{n-1} e^{-kmax^2/4eta} + ... < precision

    # The magic number 0.2 comes from AFTDF.__init__ and GDF.__init__
    eta = max(eta, 0.2)
    log_k0 = 3 + numpy.log(eta) / 2
    log_rest = numpy.log(precision / (32*numpy.pi**2*eta))
    # The interaction between two s-type density distributions should be
    # enough for the error estimation.  Put lmax here to increate Ecut for
    # slightly better accuracy
    lmax = numpy.max(cell._bas[:,gto.ANG_OF])
    Ecut = 2*eta * (log_k0*(lmax-1) - log_rest)
    Ecut = max(Ecut, .5)
    return Ecut

def estimate_eta(cell, cutoff=CUTOFF):
    '''The exponent of the smooth gaussian model density, requiring that at
    boundary, density ~ 4pi rmax^2 exp(-eta/2*rmax^2) ~ 1e-12
    '''
    lmax = min(numpy.max(cell._bas[:,gto.ANG_OF]), 4)
    # If lmax=3 (r^5 for radial part), this expression guarantees at least up
    # to f shell the convergence at boundary
    eta = max(numpy.log(4*numpy.pi*cell.rcut**(lmax+2)/cutoff)/cell.rcut**2*2,
              ETA_MIN)
    return eta

def _guess_eta(cell, kpts=None, mesh=None):
    '''Search for optimal eta and mesh'''
    if cell.dimension == 0:
        if mesh is None:
            mesh = cell.mesh
        ke_cutoff = tools.mesh_to_cutoff(cell.lattice_vectors(), mesh).min()
        eta = estimate_eta_for_ke_cutoff(cell, ke_cutoff, cell.precision)
        return eta, mesh, ke_cutoff

    # eta_min = estimate_eta_min(cell, cell.precision*1e-2)
    eta_min = ETA_MIN
    ke_min = estimate_ke_cutoff_for_eta(cell, eta_min, cell.precision)
    a = cell.lattice_vectors()

    if mesh is None:
        nkpts = len(kpts)
        ke_cutoff = 30. * nkpts**(-1./3)
        ke_cutoff = max(ke_cutoff, ke_min)
        mesh = cell.cutoff_to_mesh(ke_cutoff)
    else:
        mesh = numpy.asarray(mesh)
        mesh_min = cell.cutoff_to_mesh(ke_min)
        if numpy.any(mesh[:cell.dimension] < mesh_min[:cell.dimension]):
            logger.warn(cell, 'mesh %s is not enough to converge to the required '
                        'integral precision %g.\nRecommended mesh is %s.',
                        mesh, cell.precision, mesh_min)
    ke_cutoff = min(tools.mesh_to_cutoff(a, mesh)[:cell.dimension])
    eta = estimate_eta_for_ke_cutoff(cell, ke_cutoff, cell.precision)
    return eta, mesh, ke_cutoff

def auxbar(fused_cell):
    r'''
    Potential average = \sum_L V_L*Lpq

    The coulomb energy is computed with chargeless density
    \int (rho-C) V,  C = (\int rho) / vol = Tr(gamma,S)/vol
    It is equivalent to removing the averaged potential from the short range V
    vs = vs - (\int V)/vol * S
    '''
    aux_loc = fused_cell.ao_loc_nr()
    naux = aux_loc[-1]
    vbar = numpy.zeros(naux)
    # SR ERI should not have contributions from backgound charge
    if fused_cell.dimension < 2 or fused_cell.omega < 0:
        return vbar

    half_sph_norm = .5/numpy.sqrt(numpy.pi)
    for i in range(fused_cell.nbas):
        l = fused_cell.bas_angular(i)
        if l == 0:
            es = fused_cell.bas_exp(i)
            if es.size == 1:
                vbar[aux_loc[i]] = -1/es[0]
            else:
                # Remove the normalization to get the primitive contraction coeffcients
                norms = half_sph_norm/gto.gaussian_int(2, es)
                cs = numpy.einsum('i,ij->ij', 1/norms, fused_cell._libcint_ctr_coeff(i))
                vbar[aux_loc[i]:aux_loc[i+1]] = numpy.einsum('in,i->n', cs, -1/es)
    # TODO: fused_cell.cart and l%2 == 0: # 6d 10f ...
    # Normalization coefficients are different in the same shell for cartesian
    # basis. E.g. the d-type functions, the 5 d-type orbitals are normalized wrt
    # the integral \int r^2 * r^2 e^{-a r^2} dr.  The s-type 3s orbital should be
    # normalized wrt the integral \int r^0 * r^2 e^{-a r^2} dr. The different
    # normalization was not built in the basis.
    vbar *= numpy.pi/fused_cell.vol
    return vbar

def auxbar(fused_cell):
    r'''
    Potential average = \sum_L V_L*Lpq

    The coulomb energy is computed with chargeless density
    \int (rho-C) V,  C = (\int rho) / vol = Tr(gamma,S)/vol
    It is equivalent to removing the averaged potential from the short range V
    vs = vs - (\int V)/vol * S
    '''
    #if fused_cell is None:
    #    fused_cell, fuse = fuse_auxcell(self, self.auxcell)
    aux_loc = fused_cell.ao_loc_nr()
    vbar = numpy.zeros(aux_loc[-1])
    if fused_cell.dimension != 3:
        return vbar

    half_sph_norm = .5/numpy.sqrt(numpy.pi)
    for i in range(fused_cell.nbas):
        l = fused_cell.bas_angular(i)
        if l == 0:
            es = fused_cell.bas_exp(i)
            if es.size == 1:
                vbar[aux_loc[i]] = -1/es[0]
            else:
                # Remove the normalization to get the primitive contraction coeffcients
                norms = half_sph_norm/gto.gaussian_int(2, es)
                cs = numpy.einsum('i,ij->ij', 1/norms, fused_cell._libcint_ctr_coeff(i))
                vbar[aux_loc[i]:aux_loc[i+1]] = numpy.einsum('in,i->n', cs, -1/es)
    # TODO: fused_cell.cart and l%2 == 0: # 6d 10f ...
    # Normalization coefficients are different in the same shell for cartesian
    # basis. E.g. the d-type functions, the 5 d-type orbitals are normalized wrt
    # the integral \int r^2 * r^2 e^{-a r^2} dr.  The s-type 3s orbital should be
    # normalized wrt the integral \int r^0 * r^2 e^{-a r^2} dr. The different
    # normalization was not built in the basis.
    vbar *= numpy.pi/fused_cell.vol
    return vbar

def fuse_auxcell(mydf, auxcell):
    eta = mydf.eta
    #if eta is None:
    #    eta, mesh, ke_cutoff = _guess_eta(auxcell, mydf.kpts, mydf.mesh)   
    chgcell = make_modchg_basis(auxcell, eta)
    fused_cell = copy.copy(auxcell)
    fused_cell._atm, fused_cell._bas, fused_cell._env = \
            gto.conc_env(auxcell._atm, auxcell._bas, auxcell._env,
                         chgcell._atm, chgcell._bas, chgcell._env)
    fused_cell.rcut = max(auxcell.rcut, chgcell.rcut)

    aux_loc = auxcell.ao_loc_nr()
    naux = aux_loc[-1]
    modchg_offset = -numpy.ones((chgcell.natm,8), dtype=int)
    smooth_loc = chgcell.ao_loc_nr()
    for i in range(chgcell.nbas):
        ia = chgcell.bas_atom(i)
        l  = chgcell.bas_angular(i)
        modchg_offset[ia,l] = smooth_loc[i]

    if auxcell.cart:
        # Normalization coefficients are different in the same shell for cartesian
        # basis. E.g. the d-type functions, the 5 d-type orbitals are normalized wrt
        # the integral \int r^2 * r^2 e^{-a r^2} dr.  The s-type 3s orbital should be
        # normalized wrt the integral \int r^0 * r^2 e^{-a r^2} dr. The different
        # normalization was not built in the basis.  There two ways to surmount this
        # problem.  First is to transform the cartesian basis and scale the 3s (for
        # d functions), 4p (for f functions) ... then transform back. The second is to
        # remove the 3s, 4p functions. The function below is the second solution
        c2s_fn = gto.moleintor.libcgto.CINTc2s_ket_sph
        aux_loc_sph = auxcell.ao_loc_nr(cart=False)
        naux_sph = aux_loc_sph[-1]
        def fuse(Lpq):
            Lpq, chgLpq = Lpq[:naux], Lpq[naux:]
            if Lpq.ndim == 1:
                npq = 1
                Lpq_sph = numpy.empty(naux_sph, dtype=Lpq.dtype)
            else:
                npq = Lpq.shape[1]
                Lpq_sph = numpy.empty((naux_sph,npq), dtype=Lpq.dtype)
            if Lpq.dtype == numpy.complex128:
                npq *= 2  # c2s_fn supports double only, *2 to handle complex
            for i in range(auxcell.nbas):
                l  = auxcell.bas_angular(i)
                ia = auxcell.bas_atom(i)
                p0 = modchg_offset[ia,l]
                if p0 >= 0:
                    nd = (l+1) * (l+2) // 2
                    c0, c1 = aux_loc[i], aux_loc[i+1]
                    s0, s1 = aux_loc_sph[i], aux_loc_sph[i+1]
                    for i0, i1 in lib.prange(c0, c1, nd):
                        Lpq[i0:i1] -= chgLpq[p0:p0+nd]

                    if l < 2:
                        Lpq_sph[s0:s1] = Lpq[c0:c1]
                    else:
                        Lpq_cart = numpy.asarray(Lpq[c0:c1], order='C')
                        c2s_fn(Lpq_sph[s0:s1].ctypes.data_as(ctypes.c_void_p),
                               ctypes.c_int(npq * auxcell.bas_nctr(i)),
                               Lpq_cart.ctypes.data_as(ctypes.c_void_p),
                               ctypes.c_int(l))
            return Lpq_sph
    else:
        def fuse(Lpq):
            Lpq, chgLpq = Lpq[:naux], Lpq[naux:]
            for i in range(auxcell.nbas):
                l  = auxcell.bas_angular(i)
                ia = auxcell.bas_atom(i)
                p0 = modchg_offset[ia,l]
                if p0 >= 0:
                    nd = l * 2 + 1
                    for i0, i1 in lib.prange(aux_loc[i], aux_loc[i+1], nd):
                        Lpq[i0:i1] -= chgLpq[p0:p0+nd]
            return Lpq
    return fused_cell, fuse


class _load3c(object):
    def __init__(self, cderi, label, kpti_kptj, kptij_label=None,
                 ignore_key_error=False):
        self.cderi = cderi
        self.label = label
        if kptij_label is None:
            self.kptij_label = label + '-kptij'
        else:
            self.kptij_label = kptij_label
        self.kpti_kptj = kpti_kptj
        self.feri = None
        self.ignore_key_error = ignore_key_error

    def __enter__(self):
        self.feri = h5py.File(self.cderi, 'r')
        if self.label not in self.feri:
            # Return a size-0 array to skip the loop in sr_loop
            if self.ignore_key_error:
                return numpy.zeros(0)
            else:
                raise KeyError('Key "%s" not found' % self.label)

        kpti_kptj = numpy.asarray(self.kpti_kptj)
        kptij_lst = self.feri[self.kptij_label][()]
        return _getitem(self.feri, self.label, kpti_kptj, kptij_lst,
                        self.ignore_key_error)

    def __exit__(self, type, value, traceback):
        self.feri.close()

def _getitem(h5group, label, kpti_kptj, kptij_lst, ignore_key_error=False):
    k_id = member(kpti_kptj, kptij_lst)
    if len(k_id) > 0:
        key = label + '/' + str(k_id[0])
        if key not in h5group:
            if ignore_key_error:
                return numpy.zeros(0)
            else:
                raise KeyError('Key "%s" not found' % key)

        dat = h5group[key]
        if isinstance(dat, h5py.Group):
            # Check whether the integral tensor is stored with old data
            # foramt (v1.5.1 or older). The old format puts the entire
            # 3-index tensor in an HDF5 dataset. The new format divides
            # the tensor into pieces and stores them in different groups.
            # The code below combines the slices into a single tensor.
            dat = numpy.hstack([dat[str(i)] for i in range(len(dat))])

    else:
        # swap ki,kj due to the hermiticity
        kptji = kpti_kptj[[1,0]]
        k_id = member(kptji, kptij_lst)
        if len(k_id) == 0:
            raise RuntimeError('%s for kpts %s is not initialized.\n'
                               'You need to update the attribute .kpts then call '
                               '.build() to initialize %s.'
                               % (label, kpti_kptj, label))

        key = label + '/' + str(k_id[0])
        if key not in h5group:
            if ignore_key_error:
                return numpy.zeros(0)
            else:
                raise KeyError('Key "%s" not found' % key)

#TODO: put the numpy.hstack() call in _load_and_unpack class to lazily load
# the 3D tensor if it is too big.
        dat = _load_and_unpack(h5group[key])
    return dat

class _load_and_unpack(object):
    '''Load data lazily'''
    def __init__(self, dat):
        self.dat = dat
    def __getitem__(self, s):
        dat = self.dat
        if isinstance(dat, h5py.Group):
            v = numpy.hstack([dat[str(i)][s] for i in range(len(dat))])
        else: # For mpi4pyscf, pyscf-1.5.1 or older
            v = numpy.asarray(dat[s])

        nao = int(numpy.sqrt(v.shape[-1]))
        v1 = lib.transpose(v.reshape(-1,nao,nao), axes=(0,2,1)).conj()
        return v1.reshape(v.shape)
    def __array__(self):
        '''Create a numpy array'''
        return self[()]

    @property
    def shape(self):
        dat = self.dat
        if isinstance(dat, h5py.Group):
            all_shape = [dat[str(i)].shape for i in range(len(dat))]
            shape = all_shape[0][:-1] + (sum(x[-1] for x in all_shape),)
            return shape
        else: # For mpi4pyscf, pyscf-1.5.1 or older
            return dat.shape


def _gaussian_int(cell):
    r'''Regular gaussian integral \int g(r) dr^3'''
    return ft_ao.ft_ao(cell, numpy.zeros((1,3)))[0].real

def _round_off_to_odd_mesh(mesh):
    # Round off mesh to the nearest odd numbers.
    # Odd number of grids is preferred because even number of grids may break
    # the conjugation symmetry between the k-points k and -k.
    # When building the DF integral tensor in function _make_j3c, the symmetry
    # between k and -k is used (function conj_j2c) to overcome the error
    # caused by auxiliary basis linear dependency. More detalis of this
    # problem can be found in function _make_j3c.
    return [(i//2)*2+1 for i in mesh]

def _estimate_rcut(alpha, l, c, precision=INTEGRAL_PRECISION):
    '''rcut based on the overlap integrals. This estimation is too conservative
    in many cases. A possible replacement can be the value of the basis
    function at rcut ~ c*r^(l+2)*exp(-alpha*r^2) < precision'''
    C = (c**2)*(2*l+1) / (precision * LATTICE_SUM_PENALTY)
    r0 = 20
    # +1. to ensure np.log returning positive value
    r0 = numpy.sqrt(2.*numpy.log(C*(r0**2*alpha)**(l+1)+1.) / alpha)
    rcut = numpy.sqrt(2.*numpy.log(C*(r0**2*alpha)**(l+1)+1.) / alpha)
    return rcut
