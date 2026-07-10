def project_triangle_uv_from_pmn(a, b, c, p, m, n, epsilon=1e-12):
    """Project triangle vertices A/B/C into UV space defined by PMN basis."""
    f1 = (m[0] - p[0], m[1] - p[1], m[2] - p[2])
    f2 = (n[0] - p[0], n[1] - p[1], n[2] - p[2])

    f1_dot_f1 = (f1[0] * f1[0]) + (f1[1] * f1[1]) + (f1[2] * f1[2])
    f1_dot_f2 = (f1[0] * f2[0]) + (f1[1] * f2[1]) + (f1[2] * f2[2])
    f2_dot_f2 = (f2[0] * f2[0]) + (f2[1] * f2[1]) + (f2[2] * f2[2])

    det = (f1_dot_f1 * f2_dot_f2) - (f1_dot_f2 * f1_dot_f2)
    if abs(det) < epsilon:
        return None

    inv_det = 1.0 / det
    inv00 = f2_dot_f2 * inv_det
    inv01 = -f1_dot_f2 * inv_det
    inv11 = f1_dot_f1 * inv_det

    def project(point):
        delta = (point[0] - p[0], point[1] - p[1], point[2] - p[2])
        d0 = (f1[0] * delta[0]) + (f1[1] * delta[1]) + (f1[2] * delta[2])
        d1 = (f2[0] * delta[0]) + (f2[1] * delta[1]) + (f2[2] * delta[2])
        u = (inv00 * d0) + (inv01 * d1)
        v = (inv01 * d0) + (inv11 * d1)
        return float(u), float(v)

    return [project(a), project(b), project(c)]


def solve_pmn_from_triangle_uv(a, b, c, uv_a, uv_b, uv_c, epsilon=1e-12):
    """Solve PMN basis points from triangle A/B/C and their UVs.

    Returns (p, m, n) where each is an (x, y, z) tuple, or None if UV basis is singular.
    """
    u0, v0 = float(uv_a[0]), float(uv_a[1])
    u1, v1 = float(uv_b[0]), float(uv_b[1])
    u2, v2 = float(uv_c[0]), float(uv_c[1])

    # A matrix: rows are [u, v, 1] for each triangle corner.
    a00, a01, a02 = u0, v0, 1.0
    a10, a11, a12 = u1, v1, 1.0
    a20, a21, a22 = u2, v2, 1.0

    det = (
        a00 * (a11 * a22 - a12 * a21)
        - a01 * (a10 * a22 - a12 * a20)
        + a02 * (a10 * a21 - a11 * a20)
    )
    if abs(det) < epsilon:
        return None

    inv_det = 1.0 / det

    inv00 = (a11 * a22 - a12 * a21) * inv_det
    inv01 = (a02 * a21 - a01 * a22) * inv_det
    inv02 = (a01 * a12 - a02 * a11) * inv_det
    inv10 = (a12 * a20 - a10 * a22) * inv_det
    inv11 = (a00 * a22 - a02 * a20) * inv_det
    inv12 = (a02 * a10 - a00 * a12) * inv_det
    inv20 = (a10 * a21 - a11 * a20) * inv_det
    inv21 = (a01 * a20 - a00 * a21) * inv_det
    inv22 = (a00 * a11 - a01 * a10) * inv_det

    def solve_dim(x0, x1, x2):
        # B = inv(A) * X where B rows are e1, e2, p constants for this dimension.
        b0 = inv00 * x0 + inv01 * x1 + inv02 * x2
        b1 = inv10 * x0 + inv11 * x1 + inv12 * x2
        b2 = inv20 * x0 + inv21 * x1 + inv22 * x2
        return b0, b1, b2

    ex, fx, px = solve_dim(float(a[0]), float(b[0]), float(c[0]))
    ey, fy, py = solve_dim(float(a[1]), float(b[1]), float(c[1]))
    ez, fz, pz = solve_dim(float(a[2]), float(b[2]), float(c[2]))

    p = (px, py, pz)
    m = (px + ex, py + ey, pz + ez)
    n = (px + fx, py + fy, pz + fz)
    return p, m, n