#!/usr/bin/env python3

"""Script to generate a set of ring meshes."""

import os
import dolfin as df

GMSH_PATH = "gmsh"
GEO_NAME = "ring"

def create_mesh(exponent):
    """
    Generate a mesh using gmsh.

    Parameters
    ----------
    exponent : integer
        Parameter to supply as ``p`` to ``geo``file. Used to control mesh size
        via an exponent.

    Returns
    -------
    tuple
        ``(mesh, subdomains, boundaries)`` Often not needed because mesh is
        written anyways.

    """
    mesh_name = "{}{}".format(GEO_NAME, exponent)

    os.system(
        "{} -setnumber p {} -2 -o {}.msh {}.geo".format(
            GMSH_PATH, exponent, mesh_name, GEO_NAME))

    os.system("dolfin-convert {0}.msh {0}.xml".format(mesh_name))

    mesh = df.Mesh("{}.xml".format(mesh_name))
    subdomains = df.MeshFunction(
        "size_t", mesh, "{}_physical_region.xml".format(mesh_name))
    boundaries = df.MeshFunction(
        "size_t", mesh, "{}_facet_region.xml".format(mesh_name))

    file = df.HDF5File(mesh.mpi_comm(), "{}.h5".format(mesh_name), "w")
    file.write(mesh, "/mesh")
    file.write(subdomains, "/subdomains")
    file.write(boundaries, "/boundaries")

    return (mesh, subdomains, boundaries)

if __name__ == "__main__":
    for p in range(5, 8+1):
        create_mesh(p)
