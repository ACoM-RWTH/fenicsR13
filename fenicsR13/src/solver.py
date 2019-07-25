# pylint: disable=invalid-name

"solver module"

import dolfin as df
import ufl
import numpy as np

class Solver:
    r"""
    Solver class

    ::

        # Example usage:
        params = Input("input.yml").dict
        msh = meshes.H5Mesh("mesh.h5")
        solver = Solver(params, msh, "0") # "0" means time=0

    Assembles and solves the linear system.

    .. math::
        \mathbf{A} \mathbf{x} = \mathbf{b}

    The system results from the two dimensional, linearized R13 equations
    [TOR2003]_.

    .. [TOR2003] H Struchtrup, M Torrilhon (2003). Regularization of Grad’s 13
       moment equations: derivation and linear analysis.

    :ivar params: parameter dict
    :ivar mesh: Dolfin mesh
    :ivar cell: ``ufl_cell()`` for internal usage

    """
    def __init__(self, params, mesh, time):
        "Initializes solver"
        self.params = params
        self.mesh = mesh.mesh
        self.boundaries = mesh.boundaries
        self.cell = self.mesh.ufl_cell()
        self.time = time
        self.mode = params["mode"]
        self.use_coeffs = params["use_coeffs"]
        self.tau = params["tau"]
        self.xi_tilde = params["xi_tilde"]
        self.use_cip = self.params["stabilization"]["cip"]["enable"]
        self.delta_1 = self.params["stabilization"]["cip"]["delta_1"]
        self.delta_2 = self.params["stabilization"]["cip"]["delta_2"]
        self.delta_3 = self.params["stabilization"]["cip"]["delta_3"]
        self.bcs = self.params["bcs"]

        self.R = df.Expression(
            "sqrt(pow(x[0],2)+pow(x[1],2))", degree=2
        )
        self.phi = df.Expression("atan2(x[1],x[0])", degree=2)
        self.heat_source = df.Expression(
            self.params["heat_source"], degree=2,
            tau=self.tau, phi=self.phi, R=self.R
        )
        self.mass_source = df.Expression(
            self.params["mass_source"], degree=2,
            tau=self.tau, phi=self.phi, R=self.R
        )

        self.exact_solution = self.params["convergence_study"]["exact_solution"]
        self.output_folder = self.params["output_folder"]
        self.var_ranks = {
            "theta": 0,
            "s": 1,
            "p": 0,
            "u": 1,
            "sigma": 2,
        }
        self.elems = {
            "theta": None,
            "s": None,
            "p": None,
            "u": None,
            "sigma": None,
        }
        self.fspaces = {
            "theta": None,
            "s": None,
            "p": None,
            "u": None,
            "sigma": None,
        }
        self.mxd_elems = {
            "heat": None,
            "stress": None,
            "coupled": None,
        }
        self.mxd_fspaces = {
            "heat": None,
            "stress": None,
            "coupled": None,
        }
        self.form_a = None
        self.form_b = None
        self.sol = {
            "theta": None,
            "s": None,
            "p": None,
            "u": None,
            "sigma": None,
        }
        self.esol = {
            "theta": None,
            "s": None,
            "p": None,
            "u": None,
            "sigma": None,
        }
        self.errors = {
            "f": {
                "l2": {
                    "theta": None,
                    "s": None,
                }
            },
            "v": {
                "linf": {
                    "theta": None,
                    "s": None,
                }
            }
        }

    def setup_function_spaces(self):
        "Setup function spaces"
        cell = self.cell
        msh = self.mesh
        for var in self.elems:
            e = self.params["elements"][var]["shape"]
            deg = self.params["elements"][var]["degree"]
            if self.var_ranks[var] == 0:
                self.elems[var] = df.FiniteElement(e, cell, deg)
            elif self.var_ranks[var] == 1:
                self.elems[var] = df.VectorElement(e, cell, deg)
            elif self.var_ranks[var] == 2:
                self.elems[var] = df.TensorElement(e, cell, deg, symmetry=True)
            self.fspaces[var] = df.FunctionSpace(msh, self.elems[var])

        heat_elems = [self.elems["theta"], self.elems["s"]]
        self.mxd_elems["heat"] = df.MixedElement(heat_elems)
        self.mxd_fspaces["heat"] = df.FunctionSpace(
            msh, self.mxd_elems["heat"]
        )

        stress_elems = [self.elems["p"], self.elems["u"], self.elems["sigma"]]
        self.mxd_elems["stress"] = df.MixedElement(stress_elems)
        self.mxd_fspaces["stress"] = df.FunctionSpace(
            msh, self.mxd_elems["stress"]
        )

        self.mxd_elems["coupled"] = df.MixedElement(heat_elems, stress_elems)
        self.mxd_fspaces["coupled"] = df.FunctionSpace(
            msh, self.mxd_elems["coupled"]
        )

    def check_bcs(self):
        """
        Checks if all mesh boundaries have bcs prescribed.
        Raises an exception if something is wrong.
        """
        boundary_ids = self.boundaries.array()
        bcs_specified = list(self.bcs.keys())

        for edge_id in boundary_ids:
            if not edge_id in [0] + bcs_specified: # inner zero allowed
                raise Exception("Mesh edge id {} has no bcs!".format(edge_id))

    def assemble(self):
        "Assemble system"

        # Check if all mesh boundaries have bcs presibed frm input
        self.check_bcs()

        # Special tensor functions for 3D problems on 2D domains
        def dev3d(mat):
            "2d deviatoric part of actually 3d matrix"
            return (
                0.5 * (mat + ufl.transpose(mat))
                - (1/3) * ufl.tr(mat) * ufl.Identity(2)
            )

        def innerOfTracefree2(rank2_1, rank2_2):
            """
            Return the 3D inner prodcut of two symmetric tracefree
            rank-2 tensors (two-dimensional) as used the weak form of
            westerkamp2019.
            """
            return (
                df.inner(rank2_1, rank2_2)
                # part from u_zz=-(u_xx+u_yy) contribution
                + rank2_1[0][0] * rank2_2[0][0]
                + rank2_1[0][0] * rank2_2[1][1]
                + rank2_1[1][1] * rank2_2[0][0]
                + rank2_1[1][1] * rank2_2[1][1]
            )

        def sym3(rank3):
            """
            Returns the symmetric part of a rank-3 tensor
            Henning p231ff
            """
            i, j, k = ufl.indices(3)
            symm_ijk = 1/6 * (
                # all permutations
                + rank3[i, j, k]
                + rank3[i, k, j]
                + rank3[j, i, k]
                + rank3[j, k, i]
                + rank3[k, i, j]
                + rank3[k, j, i]
            )
            return ufl.as_tensor(symm_ijk, (i, j, k))

        def dev3(rank3):
            """
            Return the deviator of a rank-3 tensor
            Henning p231ff
            """
            i, j, k, l = ufl.indices(4)
            delta = df.Identity(3)

            sym_ijk = sym3(rank3)[i, j, k]
            trace_ijk = 1/5 * (
                + sym3(rank3)[i, l, l] * delta[j, k]
                + sym3(rank3)[j, l, l] * delta[i, k]
                + sym3(rank3)[k, l, l] * delta[i, j]
            )
            tracefree_ijk = sym_ijk - trace_ijk
            return ufl.as_tensor(tracefree_ijk, (i, j, k))

        def innerOfDevOfGrad2AndGrad2(sigma, psi):
            r"""
            Implements the inner product of the deviator of a symmetric,
            tracefree rank-2 tensor with a symmetric, tracefree rank-2 tensor.

            .. math::

                (\nabla \underline{\underline{\sigma}})_{\mathrm{dev}} :
                    \nabla \underline{\underline{\psi}}
            """
            hardcoded = False
            if hardcoded: # pylint: disable=no-else-return
                return psi[1, 1].dx(1)*((3*sigma[1, 1].dx(1))/5. - sigma[0, 1].dx(0)/5. - sigma[1, 0].dx(0)/5.) + (-psi[0, 0].dx(1) - psi[1, 1].dx(1))*(-sigma[0, 0].dx(1)/3. - (7*sigma[1, 1].dx(1))/15. - sigma[0, 1].dx(0)/15. - sigma[1, 0].dx(0)/15.) + psi[0, 0].dx(1)*(sigma[0, 0].dx(1)/3. - (2*sigma[1, 1].dx(1))/15. + (4*sigma[0, 1].dx(0))/15. + (4*sigma[1, 0].dx(0))/15.) + psi[0, 1].dx(1)*((4*sigma[0, 1].dx(1))/15. + (4*sigma[1, 0].dx(1))/15. - (2*sigma[0, 0].dx(0))/15. + sigma[1, 1].dx(0)/3.) + psi[1, 0].dx(1)*((4*sigma[0, 1].dx(1))/15. + (4*sigma[1, 0].dx(1))/15. - (2*sigma[0, 0].dx(0))/15. + sigma[1, 1].dx(0)/3.) + (-sigma[0, 1].dx(1)/5. - sigma[1, 0].dx(1)/5. + (3*sigma[0, 0].dx(0))/5.)*psi[0, 0].dx(0) + (sigma[0, 0].dx(1)/3. - (2*sigma[1, 1].dx(1))/15. + (4*sigma[0, 1].dx(0))/15. + (4*sigma[1, 0].dx(0))/15.)*psi[0, 1].dx(0) + (sigma[0, 0].dx(1)/3. - (2*sigma[1, 1].dx(1))/15. + (4*sigma[0, 1].dx(0))/15. + (4*sigma[1, 0].dx(0))/15.)*psi[1, 0].dx(0) + (-sigma[0, 1].dx(1)/15. - sigma[1, 0].dx(1)/15. - (7*sigma[0, 0].dx(0))/15. - sigma[1, 1].dx(0)/3.)*(-psi[0, 0].dx(0) - psi[1, 1].dx(0)) + ((4*sigma[0, 1].dx(1))/15. + (4*sigma[1, 0].dx(1))/15. - (2*sigma[0, 0].dx(0))/15. + sigma[1, 1].dx(0)/3.)*psi[1, 1].dx(0)
            else:
                return df.inner(
                    dev3(grad3dOf2(gen3dTracefreeTensor(sigma))),
                    grad3dOf2(gen3dTracefreeTensor(psi))
                    # dev3(grad3dOf2(gen3dTracefreeTensor(psi))) # same
                )

        def gen3dTracefreeTensor(rank2):
            r"""
            Returns the synthetic 3D version
            :math:`A \in \mathbb{R}^{3 \times 3}`
            of a 2D rank-2 tensor
            :math:`B \in \mathbb{R}^{2 \times 2}`.

            .. math::

                B = \begin{pmatrix}
                        b_{xx} & b_{xy} \
                        b_{yx} & b_{yy} \
                    \end{pmatrix}

                A = \begin{pmatrix}
                        b_{xx} & b_{xy} & 0                \
                        b_{yx} & b_{yy} & 0                \
                        0      & 0      & -(b_{yx}+b_{yy}) \
                    \end{pmatrix}
            """
            return df.as_tensor([
                [rank2[0, 0], rank2[0, 1], 0],
                [rank2[1, 0], rank2[1, 1], 0],
                [0, 0, -rank2[0, 0]-rank2[1, 1]]
            ])

        def grad3dOf2(rank2):
            """
            Returns the 3D version gradient of a 3D synthetic tracefree tensor,
            created from a 2D rank-2 tensor.
            """
            grad2d = df.grad(rank2)
            dim3 = df.as_tensor([
                [0, 0, 0],
                [0, 0, 0],
                [0, 0, 0],
            ])
            grad3d = df.as_tensor([
                grad2d[:, :, 0], # pylint: disable=unsubscriptable-object
                grad2d[:, :, 1], # pylint: disable=unsubscriptable-object
                dim3[:, :]
            ])
            return grad3d

        # Local variables
        mesh = self.mesh
        boundaries = self.boundaries
        bcs = self.bcs
        tau = self.tau
        xi_tilde = self.xi_tilde
        delta_1 = self.delta_1
        delta_2 = self.delta_2
        delta_3 = self.delta_3

        # Normal and tangential components
        # => tangential (tx,ty) = (-ny,nx) = perp(n) only for 2D
        n = df.FacetNormal(mesh)
        t = ufl.perp(n)

        # Define custom measeasure for boundaries
        df.ds = df.Measure("ds", domain=mesh, subdomain_data=boundaries)
        df.dS = df.Measure("dS", domain=mesh, subdomain_data=boundaries)

        h = df.CellDiameter(mesh)
        h_avg = (h("+") + h("-"))/2.0 # pylint: disable=not-callable

        if self.mode == "heat":

            w = self.mxd_fspaces["heat"]

            # Define trial and testfunction
            (theta, s) = df.TrialFunctions(w)
            (kappa, r) = df.TestFunctions(w)

            s_n = df.dot(s, n)
            r_n = df.dot(r, n)
            s_t = df.dot(s, t)
            r_t = df.dot(r, t)

            # Define heat source function
            f = self.heat_source

            if self.use_coeffs:
                a1 = (
                    + 12/5 * tau * df.inner(dev3d(df.grad(s)), df.grad(r))
                    + 2/3 * (1/tau) * df.inner(s, r)
                    - (5/2) * theta * df.div(r)
                ) * df.dx + (
                    + 5/(4*xi_tilde) * s_n * r_n
                    + 11/10 * xi_tilde * s_t * r_t
                ) * df.ds
                a2 = - (df.div(s) * kappa) * df.dx
                l1 = sum([
                    - 5.0/2.0 * r_n * bcs[bc]["theta_w"] * df.ds(bc)
                    for bc in bcs.keys()
                ])
                l2 = - (f * kappa) * df.dx
            else:
                a1 = (
                    tau * df.inner(dev3d(df.grad(s)), df.grad(r))
                    + (1/tau) * df.inner(s, r)
                    - theta * df.div(r)
                ) * df.dx + (
                    + 1/(xi_tilde) * s_n * r_n
                    + xi_tilde * s_t * r_t
                ) * df.ds
                a2 = - (df.div(s) * kappa) * df.dx
                l1 = sum([
                    - 1 * r_n * bcs[bc]["theta_w"] * df.ds(bc)
                    for bc in bcs.keys()
                ])
                l2 = - (f * kappa) * df.dx

            # stabilization
            if self.use_cip:
                stab = - (delta_1 * h_avg**3 * df.jump(df.grad(theta), n)
                          * df.jump(df.grad(kappa), n)) * df.dS
            else:
                stab = 0

            self.form_a = a1 + a2 + stab
            self.form_b = l1 + l2

        elif self.mode == "stress":

            w = self.mxd_fspaces["stress"]

            # Define trial and testfunctions
            (p, u, sigma) = df.TrialFunctions(w)
            (q, v, psi) = df.TestFunctions(w)

            sigma_nn = df.dot(sigma*n, n)
            psi_nn = df.dot(psi*n, n)
            sigma_tt = df.dot(sigma*t, t)
            psi_tt = df.dot(psi*t, t)
            sigma_nt = df.dot(sigma*n, t)
            psi_nt = df.dot(psi*n, t)

            # Define mass source function
            f = self.mass_source

            if self.use_coeffs:
                a1 = (
                    # + 2 * tau * df.inner(devOfGrad2(sigma), df.grad(psi))

                    + 2 * tau * innerOfDevOfGrad2AndGrad2(sigma, psi)
                    + (1/tau) * innerOfTracefree2(sigma, psi)
                    # + (1/tau) * df.inner(sigma, psi) # is wrong
                    # - 2 * df.dot(u, df.div(psi)) # is same
                    - 2 * df.dot(u, df.div(df.sym(psi)))
                ) * df.dx + (
                    + 21/(10*xi_tilde) * sigma_nn * psi_nn
                    + 2 * xi_tilde * (
                        (sigma_tt + (1/2)*sigma_nn)*(psi_tt + (1/2)*psi_nn)
                    )
                    + (2/xi_tilde) * sigma_nt * psi_nt
                ) * df.ds
                l1 = sum([
                    - 2.0 * psi_nt * bcs[bc]["v_t"] * df.ds(bc)
                    for bc in bcs.keys()
                ])
                a2 = +(df.dot(df.div(sigma), v) + df.dot(df.grad(p), v)) * df.dx
                l2 = +df.Constant(0) * df.div(v) * df.dx # dummy
                a3 = +df.dot(u, df.grad(q)) * df.dx
                l3 = -(f * q) * df.dx

            if self.use_cip:
                stab = (
                    + delta_2 * h_avg**3 *
                    df.dot(df.jump(df.grad(u), n), df.jump(df.grad(v), n))
                    - delta_3 * h_avg *
                    df.jump(df.grad(p), n) * df.jump(df.grad(q), n)
                    ) * df.dS
            else:
                stab = 0

            self.form_a = a1 + a2 + a3 + stab
            self.form_b = l1 + l2 + l3

    def solve(self):
        """
        Solves the system.
        Some available solver options:

        .. code-block:: python

            solver_parameters={
                'linear_solver': 'gmres', 'preconditioner': 'ilu'
            }
            solver_parameters={
                'linear_solver': 'petsc', 'preconditioner': 'ilu'
            }
            solver_parameters={'linear_solver': 'direct'}
            solver_parameters={'linear_solver': 'mumps'}

        """
        if self.mode == "heat":

            w = self.mxd_fspaces["heat"]
            sol = df.Function(w)
            df.solve(
                self.form_a == self.form_b, sol, [],
                solver_parameters={"linear_solver": "mumps"}
            )

            (self.sol["theta"], self.sol["s"]) = sol.split()
        elif self.mode == "stress":

            w = self.mxd_fspaces["stress"]
            sol = df.Function(w)
            df.solve(
                self.form_a == self.form_b, sol, [],
                solver_parameters={"linear_solver": "direct"}
            )

            (self.sol["p"], self.sol["u"], self.sol["sigma"]) = sol.split()

            # Scale pressure to have zero mean
            p_i = df.interpolate(self.sol["p"], self.fspaces["p"])
            mean_p_value = self.calc_sf_mean(p_i)
            mean_p_fct = df.Function(self.fspaces["p"])
            mean_p_fct.assign(df.Constant(mean_p_value))
            p_i.assign(p_i - mean_p_fct)
            self.sol["p"] = p_i

    def load_exact_solution(self):
        "Writes exact solution"
        if self.mode == "heat":

            with open(self.exact_solution, "r") as file:
                exact_solution_cpp_code = file.read()

            esol = df.compile_cpp_code(exact_solution_cpp_code)

            self.esol["theta"] = df.CompiledExpression(
                esol.Temperature(), degree=2
            )

            self.esol["s"] = df.CompiledExpression(
                esol.Heatflux(), degree=2
            )
        if self.mode == "stress":

            with open(self.exact_solution, "r") as file:
                exact_solution_cpp_code = file.read()

            esol = df.compile_cpp_code(exact_solution_cpp_code)

            self.esol["p"] = df.CompiledExpression(
                esol.Pressure(), degree=2
            )

            self.esol["u"] = df.CompiledExpression(
                esol.Velocity(), degree=2
            )

            self.esol["sigma"] = df.CompiledExpression(
                esol.Stress(), degree=2
            )

    def calc_sf_mean(self, scalar_function):
        """
        Calculates the mean of a scalar function.

        .. code-block:: python

            np.set_printoptions(precision=16)
            print(mean) # precision is not soo nice, only 9 digits
            print(self.calc_sf_mean(self.sol["p"])) # in solve() has m. prec. hm
        """
        mean = np.mean(scalar_function.vector()[:], dtype=np.float64)
        return mean


    def calc_errors(self):
        "Calculate errors"

        def calc_scalarfield_errors(sol_, sol_e_, v_sol, name_):
            "TODO"

            field_e_i = df.interpolate(sol_e_, v_sol)
            field_i = df.interpolate(sol_, v_sol)

            difference = df.project(sol_e_ - sol_, v_sol)
            self.write_xdmf("difference_{}".format(name_), difference)

            err_f_L2 = df.errornorm(sol_e_, sol_, "L2")
            err_v_linf = df.norm(field_e_i.vector()-field_i.vector(), "linf")
            print("L_2 error:", err_f_L2)
            print("l_inf error:", err_v_linf)

            self.write_xdmf(name_ + "_e", field_e_i)

            return (err_f_L2, err_v_linf)

        def calc_vectorfield_errors(sol_, sol_e_, v_sol, name_):
            "TODO"

            field_e_i = df.interpolate(sol_e_, v_sol)
            field_i = df.interpolate(sol_, v_sol)

            difference = df.project(sol_e_ - sol_, v_sol)
            self.write_xdmf("difference_{}".format(name_), difference)

            dofs = len(field_e_i.split())
            errs_f_L2 = [df.errornorm(
                field_e_i.split()[i], field_i.split()[i], "L2"
            ) for i in range(dofs)] # ignore warning
            errs_v_linf = [
                np.max(
                    np.abs(
                        field_e_i.split()[i].compute_vertex_values()
                        - field_i.split()[i].compute_vertex_values()
                    )
                )
                for i in range(dofs)
            ]
            print("L_2 error:", errs_f_L2)
            print("l_inf error:", errs_v_linf)

            self.write_xdmf(name_ + "_e", field_e_i)

            return (errs_f_L2, errs_v_linf)

        def calc_tensorfield_errors(sol_, sol_e_, v_sol, name_):
            "TODO"

            field_e_i = df.interpolate(sol_e_, v_sol)
            field_i = df.interpolate(sol_, v_sol)

            # difference = df.project(sol_e_ - sol_, v_sol) # different outpu
            difference = df.project(field_e_i - field_i, v_sol)
            self.write_xdmf("difference_{}".format(name_), difference)

            dofs = len(field_e_i.split())
            errs_f_L2 = [df.errornorm(
                field_e_i.split()[i], field_i.split()[i], "L2"
            ) for i in range(dofs)] # ignore warning
            errs_v_linf = [
                np.max(
                    np.abs(
                        field_e_i.split()[i].compute_vertex_values()
                        - field_i.split()[i].compute_vertex_values()
                    )
                )
                for i in range(dofs)
            ]
            print("L_2 error:", errs_f_L2)
            print("l_inf error:", errs_v_linf)

            self.write_xdmf(name_ + "_e", field_e_i)

            return (errs_f_L2, errs_v_linf)

        if self.mode == "heat":
            se = calc_scalarfield_errors(
                self.sol["theta"], self.esol["theta"],
                self.fspaces["theta"], "theta"
            )
            ve = calc_vectorfield_errors(
                self.sol["s"], self.esol["s"],
                self.fspaces["s"], "s"
            )
            f_l2 = self.errors["f"]["l2"]
            v_linf = self.errors["v"]["linf"]
            (f_l2["theta"], v_linf["theta"]) = se
            (f_l2["s"], v_linf["s"]) = ve
        if self.mode == "stress":
            se = calc_scalarfield_errors(
                self.sol["p"], self.esol["p"],
                self.fspaces["p"], "p"
            )
            ve = calc_vectorfield_errors(
                self.sol["u"], self.esol["u"],
                self.fspaces["u"], "u"
            )
            te = calc_tensorfield_errors(
                self.sol["sigma"], self.esol["sigma"],
                self.fspaces["sigma"], "sigma"
            )
            f_l2 = self.errors["f"]["l2"]
            v_linf = self.errors["v"]["linf"]
            (f_l2["p"], v_linf["p"]) = se
            (f_l2["u"], v_linf["u"]) = ve
            (f_l2["sigma"], v_linf["sigma"]) = te

    def write_solutions(self):
        "Write Solutions"
        sols = self.sol
        for field in sols:
            if sols[field] is not None:
                self.write_xdmf(field, sols[field])

    def write_parameters(self):
        "Write Parameters: Heat source or Mass Source"

        el = "Lagrange"
        deg = 1

        # Heat source
        f_heat = df.interpolate(
            self.heat_source,
            df.FunctionSpace(
                self.mesh,
                df.FiniteElement(el, degree=deg, cell=self.cell)
            )
        )
        self.write_xdmf("f_heat", f_heat)

        # Mass source
        f_mass = df.interpolate(
            self.mass_source,
            df.FunctionSpace(
                self.mesh,
                df.FiniteElement(el, degree=deg, cell=self.cell)
            )
        )
        self.write_xdmf("f_mass", f_mass)


    def write_xdmf(self, name, field):
        "Writes a renamed field to XDMF format"
        filename = self.output_folder + name + "_" + str(self.time) + ".xdmf"
        with df.XDMFFile(self.mesh.mpi_comm(), filename) as file:

            for degree in range(5): # test until degree five
                # Writing symmetric tensors crashes.
                # Therefore project symmetric tensor in nonsymmetric space
                # This is only a temporary fix, see:
                # https://fenicsproject.discourse.group/t/...
                # ...writing-symmetric-tensor-function-fails/1136
                el_symm = df.TensorElement(
                    df.FiniteElement(
                        "Lagrange", df.triangle, degree+1
                    ), symmetry=True
                ) # symmetric tensor element
                el_sol = field.ufl_function_space().ufl_element()
                if el_sol == el_symm:
                    # Remove symmetry with projection
                    field = df.project(
                        field, df.TensorFunctionSpace(
                            self.mesh, "Lagrange", degree+1
                        )
                    )
                    break

            field.rename(name, name)
            file.write(field, self.time)

    def write_systemmatrix(self):
        """
        Writes system matrix. Can be used to analyze e.g. condition number with
        decreasing mesh sizes.

        Import to Matlab with:

        .. code-block:: matlab

            % First adapt to filename...
            T = readtable("a.txt");
            M = table2array(T);

        """

        np.savetxt(
            self.output_folder + "A_{}.txt".format(self.time),
            df.assemble(self.form_a).array()
        )
