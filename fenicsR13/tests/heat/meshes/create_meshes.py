"Script to generate a set of ring meshes"

import os
import dolfin as df

GMSH_PATH = "gmsh"
GEO_NAME = "ring"

def create_mesh(exponent):
    "Generates a mesh using gmsh"

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

for p in range(5):
    create_mesh(p)