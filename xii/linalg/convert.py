from xii.linalg.matrix_utils import (is_petsc_vec, is_petsc_mat, diagonal_matrix,
                                     is_number, as_petsc, petsc_serial_matrix,
                                     zero_matrix)

from block.block_compose import block_mul, block_add, block_sub, block_transpose
from block import block_mat, block_vec
from dolfin import PETScVector, PETScMatrix, mpi_comm_world
from scipy.sparse import bmat as numpy_block_mat
from scipy.sparse import csr_matrix
from petsc4py import PETSc
import numpy as np
import itertools
import operator


def convert(bmat, algorithm='numpy'):
    '''
    Attempt to convert bmat to a PETSc(Matrix/Vector) object.
    If succed this is at worst a number.
    '''
    # Block vec conversion
    if isinstance(bmat, block_vec):
        array = block_vec_to_numpy(bmat)
        vec = PETSc.Vec().createWithArray(array)
        return PETScVector(vec)
    
    # Conversion of bmat is bit more involved because of the possibility
    # that some of the blocks are numbers or composition of matrix operations
    if isinstance(bmat, block_mat):
        # Create collpsed bmat
        row_sizes, col_sizes = bmat_sizes(bmat)
        nrows, ncols = len(row_sizes), len(col_sizes)
        indices = itertools.product(range(nrows), range(ncols))
        
        blocks = np.zeros((nrows, ncols), dtype='object')
        for block, (i, j) in zip(bmat.blocks.flatten(), indices):
            # This might is guaranteed to be matrix or number
            A = collapse(block)
            # Only numbers on the diagonal block are interpresented as
            # scaled identity matrices
            if is_number(A):
                if A != 0:
                    assert row_sizes[i] == col_sizes[j]
                    A = diagonal_matrix(row_sizes[i], A)
                else:
                    A = 0
            # The converted block
            blocks[i, j] = A
        # Now every block is a matrix/number and we can make a monolithic thing
        bmat = block_mat(blocks)

        assert all(is_petsc_mat(block) or is_number(block)
                   for block in bmat.blocks.flatten())
        # Opt out of monolithic
        if not algorithm: return bmat
        
        # Monolithic via numpy (fast)
        # Convert to numpy
        array = block_mat_to_numpy(bmat)
        # Constuct from numpy
        return numpy_to_petsc(array)

    # Try with a composite
    return collapse(bmat)


def collapse(bmat):
    '''Collapse what are blocks of bmat'''
    # Single block cases
    # Do nothing
    if is_petsc_mat(bmat) or is_number(bmat) or is_petsc_vec(bmat):
        return bmat

    # Multiplication
    if isinstance(bmat, block_mul):
        return collapse_mul(bmat)
    # +
    elif isinstance(bmat, block_add):
        return collapse_add(bmat)
    # -
    elif isinstance(bmat, block_sub):
        return collapse_sub(bmat)
    # T
    elif isinstance(bmat, block_transpose):
        return collapse_tr(bmat)
    # Some things in cbc.block know their matrix representation
    # E.g. InvLumpDiag...
    elif hasattr(bmat, 'A'):
        assert is_petsc_mat(bmat.A)
        return bmat.A

    raise ValueError('Do not know how to collapse %r' % type(bmat))


def collapse_tr(bmat):
    '''to Transpose'''
    # Base
    A = bmat.A
    if is_petsc_mat(A):
        A_ = as_petsc(A)
        C_ = PETSc.Mat()
        A_.transpose(C_)
        return PETScMatrix(C_)
    # Recurse
    return collapse_tr(collapse(bmat))


def collapse_add(bmat):
    '''A + B to single matrix'''
    A, B = bmat.A, bmat.B
    # Base case
    if is_petsc_mat(A) and is_petsc_mat(B):
        A_ = as_petsc(A)
        B_ = as_petsc(B)
        assert A_.size == B_.size
        C_ = A_.copy()
        # C = A + B
        C_.axpy(1., B_, PETSc.Mat.Structure.DIFFERENT)
        return PETScMatrix(C_)
    # Recurse
    return collapse_add(collapse(A) + collapse(B))


def collapse_sub(bmat):
    '''A - B to single matrix'''
    A, B = bmat.A, bmat.B
    # Base case
    if is_petsc_mat(A) and is_petsc_mat(B):
        A_ = as_petsc(A)
        B_ = as_petsc(B)
        assert A_.size == B_.size
        C_ = A_.copy()
        # C = A - B
        C_.axpy(-1., B_, PETSc.Mat.Structure.DIFFERENT)
        return PETScMatrix(C_)
    # Recurse
    return collapse_sub(collapse(A) - collapse(B))


def collapse_mul(bmat):
    '''A*B*C to single matrix'''
    # A0 * A1 * ...
    A, B = bmat.chain[0], bmat.chain[1:]

    if len(B) == 1:
        B = B[0]
        # Two matrices
        if is_petsc_mat(A) and is_petsc_mat(B):
            A_ = as_petsc(A)
            B_ = as_petsc(B)
            assert A_.size[1] == B_.size[0]
            C_ = PETSc.Mat()
            A_.matMult(B_, C_)

            return PETScMatrix(C_)
        # One of them is a number
        elif is_petsc_mat(A) and is_number(B):
            A_ = as_petsc(A)
            C_ = A_.copy()
            C_.scale(B)
            return PETScMatrix(C_)

        elif is_petsc_mat(B) and is_number(A):
            B_ = as_petsc(B)
            C_ = B_.copy()
            C_.scale(A)
            return PETScMatrix(C_)
        # Some compositions
        else:
            return collapse(collapse(A)*collapse(B))
    # Recurse
    else:
        return collapse_mul(collapse(A)*collapse(reduce(operator.mul, B)))                                    

    
# Conversion via numpy
def block_vec_to_numpy(bvec):
    '''Collapsing block bector to numpy array'''
    return np.hstack([v.get_local() for v in bvec])


def block_mat_to_numpy(bmat):
    '''Collapsing block mat of matrices to scipy's bmat'''
    # A single matrix
    if is_petsc_mat(bmat):
        bmat = as_petsc(bmat)
        return csr_matrix(bmat.getValuesCSR()[::-1], shape=bmat.size)
    # 0
    if is_number(bmat):
        return None  # What bmat accepts
    # Recurse on blocks
    blocks = np.array(map(block_mat_to_numpy, bmat.blocks.flatten()))
    blocks = blocks.reshape(bmat.blocks.shape)
    # The bmat
    return numpy_block_mat(blocks).tocsr()


def numpy_to_petsc(mat):
    '''Build PETScMatrix with array structure'''
    # Dense array to matrix
    if isinstance(mat, np.ndarray):
        return numpy_to_petsc(csr_matrix(mat))
    # Sparse
    A = PETSc.Mat().createAIJ(size=mat.shape,
                              csr=(mat.indptr, mat.indices, mat.data)) 
    return PETScMatrix(A)


def block_mat_to_petsc(bmat):
    '''Block mat to PETScMatrix via assembly'''
    # This is beautiful but slow as hell :)
    def iter_rows(matrix):
        for i in range(matrix.size(0)):
            yield matrix.getrow(i)

    row_sizes, col_sizes = get_sizes(bmat)
    row_offsets = np.cumsum([0] + list(row_sizes))
    col_offsets = np.cumsum([0] + list(col_sizes))

    with petsc_serial_matrix(row_offsets[-1], col_offsets[-1]) as mat:
        row = 0
        for row_blocks in bmat.blocks:
            # Zip the row iterators of the matrices together
            for indices_values in itertools.izip(*map(iter_rows, row_blocks)):
                indices, values = zip(*indices_values)

                indices = [list(index+offset) for index, offset in zip(indices, col_offsets)]
                indices = sum(indices, [])
            
                row_values = np.hstack(values)

                mat.setValues([row], indices, row_values, PETSc.InsertMode.INSERT_VALUES)

                row += 1
    return PETScMatrix(mat)


def get_dims(thing):
    '''
    Size of Rn vector or operator Rn to Rm. We return None for scalars
    and raise when such an operator cannot be established, i.e. there 
    are consistency checks going on 
    '''
    if is_petsc_vec(thing): return thing.size()

    if is_petsc_mat(thing): return (thing.size(0), thing.size(1))
    
    if is_number(thing): return None
    
    # Now let's handdle block stuff
    # Multiplication
    if isinstance(thing, block_mul):
        A, B = thing.chain[0], thing.chain[1:]

        dims_A, dims_B = get_dims(A), get_dims(B[0])
        # A number does not change
        if dims_A is None:
            return dims_B
        if dims_B is None:
            return dims_A
        # Otherwise, consistency
        if len(B) == 1:
            assert len(dims_A) == len(dims_B) 
            assert dims_A[1] == dims_B[0]  
            return (dims_A[0], dims_B[1])
        else:
            dims_B = get_dims(reduce(operator.mul, B))
            
            assert len(dims_A) == len(dims_B) 
            assert dims_A[1] == dims_B[0]  
            return (dims_A[0], dims_B[1])
    # +, -
    elif isinstance(thing, (block_add, block_sub)):
        A, B = thing.A, thing.B
        if is_number(A):
            return get_dims(B)

        if is_number(B):
            return get_dims(A)

        dims = get_dims(A)
        assert dims == get_dims(B), (dims, get_dims(B))
        return dims
    # T
    elif isinstance(thing, block_transpose):
        dims = get_dims(thing.A)
        return (dims[1], dims[0])
    # Some things in cbc.block know their matrix representation
    # E.g. InvLumpDiag...
    elif hasattr(thing, 'A'):
        assert is_petsc_mat(thing.A)
        return get_dims(thing.A)

    raise ValueError('Cannot get_dims of %r, %s' % (type(thing), thing))

    
def bmat_sizes(bmat):
    '''Return a tuple which represents sizes of (blocks of) bmat'''
    if isinstance(bmat, block_vec):
        return tuple(map(get_dims, block_vec.blocks))

    if isinstance(bmat, block_mat):
        bmat = bmat.blocks
        row_sizes , col_sizes = [], []
        for row in bmat:
            # In each row there must be something that be used to get
            # the row size
            size = set([dim[0]
                        for dim in [get_dims(A) for A in row]
                        if dim is not None])
            # Moreover all the things should agree on the row size
            # (since they are in the same row)
            assert len(size) == 1, size
            row_sizes.append(size.pop())

        for col in bmat.T:
            size = set([dim[1]
                        for dim in [get_dims(A) for A in col]
                        if dim is not None])
            
            assert len(size) == 1, size
            col_sizes.append(size.pop())
            
        # However we return both to indicate this was matrix like
        return (tuple(row_sizes), tuple(col_sizes))

    raise ValueError('Cannot bmat_sizes of %r, %s' % (type(bmat), bmat))


# -------------------------------------------------------------------


if __name__ == '__main__':
    from dolfin import *

    mesh = UnitSquareMesh(32, 32)
    V = FunctionSpace(mesh, 'CG', 1)
    Q = FunctionSpace(mesh, 'DG', 0)
    W = [V, Q]
    
    u, p = map(TrialFunction, W)
    v, q = map(TestFunction, W)

    [[A00, A01],
     [A10, A11]] = [[assemble(inner(u, v)*dx), assemble(inner(v, p)*dx)],
                    [assemble(inner(u, q)*dx), assemble(inner(p, q)*dx)]]
    blocks = [[A00*A00, A01+A01],
              [2*A10 - A10, A11*A11*A11]]
    
    AA = block_mat(blocks)

    t = Timer('x'); t.start()
    X = convert(AA)
    print t.stop()

    t = Timer('x'); t.start()
    Y = convert(AA, 'foo')
    print t.stop()

    X_ = X.array()
    X_[:] -= Y.array()
    print np.linalg.norm(X_, np.inf)
