from dolfin import Function, as_backend_type, PETScVector
from block import block_vec
from petsc4py import PETSc


first = lambda iterable: next(iter(iterable))


def as_petsc_nest(bvec):
    '''Represent bvec as PETSc nested vector'''
    assert isinstance(bvec, block_vec)
    nest = [as_backend_type(v).vec() for v in bvec]
    return PETSc.Vec().createNest(nest)


class ii_Function(object):
    '''Really a list of functions where each is in some W[i]'''
    def __init__(self, W, components=None):
        if components is None:
            self.functions = map(Function, W)
        else:
            assert len(components) == len(W)
            # Functions them selves
            if hasattr(first(components), 'function_space'):
                assert [c.function_space() == Wi for c, Wi in zip(components, W)]
                self.functions = [ci for ci in c]                
            # The components can be vectors in that case
            else:
                # Dim check
                assert all(c.size() == Wi.dim() for c, Wi in zip(components, W))
                # Create
                self.functions = [Function(Wi, c) for c, Wi in zip(components, W)]

    def vectors(self):
        '''Coefficient vectors of the functions I hold'''
        return [f.vector() for f in self.functions]

    def vector(self):
        '''
        A PETSc vector which is wired up with coefficient vectors of 
        the components. So change to component changes this and vice 
        versa
        '''
        return PETScVector(self.petsc_vec())

    def petsc_vec(self):
        '''PETSc Vec (not dolfin.PETSc!)'''
        return as_petsc_nest(self.block_vec())
        
    def block_vec(self):
        '''A block vec that is the coefficients of the function'''
        return block_vec(self.vectors())

    def __len__(self): return len(self.functions)
    
    def __getitem__(self, i):
        '''Get the function in the ith subspace'''
        assert 0 <= i < len(self), (i, len(self))
        return self.functions[i]

    def __iter__(self):
        for i in range(len(self)): yield self[i]
