import argparse
import sys

import numpy as np
from scipy.optimize import least_squares

parser = argparse.ArgumentParser()
parser.add_argument('--xs', nargs='+', default=[], type=float)
parser.add_argument('--ys', nargs='+', default=[], type=float)
parser.add_argument('--tol_quad', type=float)
parser.add_argument('--slope_min', type=float)
parser.add_argument('--slope_max', type=float)
args = parser.parse_args()


def check_linear_fit(xs: list[float], ys: list[float], tol_quad: float, slope_min: float, slope_max: float) -> bool:
    """return true if the fitted slope is between slope_min and slope_max

    Args:
        xs (list[float]): x values
        ys (list[float]): y values
        tol_quad (float): tolerance quad value
        slope_min (float): minimum slope
        slope_max (float): maximum slope

    Returns:
        bool: true if the fitted slope is between slope_min and slope_max
    """
    if slope_min >= slope_max:
        print("slope_min should be less than slope_max")
        print(f"slope_min: {slope_min} >= slope_max: {slope_max}")
        return False
    # Fit the data to a quadratic model and check that the
    # leading coefficient is zero (within some tolerance).
    # check the fitted slope is within (slope_min, slope_max).

    def quadratic(params: np.ndarray, x: float) -> float:
        return float(params[0]*x*x + params[1]*x + params[2])

    def residuals(params: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        ys_model = np.array([quadratic(params, x) for x in xs])
        res: np.ndarray = ys - ys_model
        return res

    xs_np = np.array(xs)
    ys_np = np.array(ys)
    if len(xs_np) != len(ys_np):
        print('Warning! length of xs != length of ys')
        return False

    init_guess_linear = [0.0, 1.0, 0.0]  # y = x
    result = least_squares(residuals, init_guess_linear, args=(xs_np, ys_np))

    if not result.success:
        print('Warning! least_squares optimization did not succeed!')
        return False

    is_linear = abs(result.x[0]) < tol_quad
    if not is_linear:
        print('Warning! fit is not linear!')
        print(f'abs(result.x[0]): {abs(result.x[0])} >= tol_quad: {tol_quad}')
        return False

    slope_fit = result.x[1]
    is_within_bounds: bool = slope_min < slope_fit and slope_fit < slope_max
    print(f'result.x[0]: {result.x[0]}, result.x[1]: {result.x[1]} (slope_fit)')
    print(f'slope_fit: {slope_fit}, slope_min: {slope_min}, slope_max: {slope_max}')
    if not is_within_bounds:
        print('Warning! slope_fit is out of the bounds (slope_min, slope_max)')
        return False
    else:
        return True


if __name__ == '__main__':
    is_linear_and_within_bounds = check_linear_fit(args.xs, args.ys, args.tol_quad, args.slope_min, args.slope_max)
    if is_linear_and_within_bounds:
        sys.exit(0)
    else:
        sys.exit(1)
