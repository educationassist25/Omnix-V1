"""
normalization.py - ISTD normalization, IQR normalization, and strict Log2 transformation.

Log2 transformation is strictly:

    log2(x)

No pseudocount, constant addition, zero replacement, shifting, or arbitrary offset
is applied. Values <= 0 cannot be log2 transformed and are converted to NaN.
"""

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# 1. ISTD normalization
# ---------------------------------------------------------------------------
def istd_normalize(peak_df: pd.DataFrame, istd_name: str) -> pd.DataFrame:
    """
    Internal Standard (ISTD) normalization.

    Normalized Peak Area =
        Endogenous Metabolite Peak Area / Internal Standard Peak Area

    The ISTD feature itself is removed from the returned dataframe.

    If an ISTD value is zero, division is undefined and the corresponding
    normalized values become NaN.
    """
    if istd_name not in peak_df.index:
        raise ValueError(
            f"Internal standard '{istd_name}' not found among features."
        )

    istd_row = peak_df.loc[istd_name]

    # Avoid division by zero.
    denominator = istd_row.replace(0, np.nan)

    normalized = (
        peak_df
        .drop(index=istd_name)
        .div(denominator, axis=1)
    )

    return normalized


def istd_normalize_multi(peak_df: pd.DataFrame, istd_names, row_annotations: pd.DataFrame = None):
    """
    ISTD normalization supporting one OR MULTIPLE internal standards (e.g. up to
    20 class-specific lipid ISTDs in targeted lipidomics, where each lipid class
    is conventionally normalized to its own class-matched internal standard).

    - A single ISTD behaves exactly like `istd_normalize()`.
    - Multiple ISTDs: each non-ISTD feature is matched to the internal standard
      that shares its lipid class, using a `row_annotations` 'Class' column
      (matched for both the feature and every candidate ISTD row). A feature
      that can't be matched this way (no row annotations, no 'Class' column,
      or no ISTD sharing its class) falls back to a pooled internal standard:
      the per-sample geometric mean across all selected ISTDs.

    Returns (normalized_df, unmatched_features) where unmatched_features lists
    every feature that fell back to the pooled ISTD (empty for a single ISTD).
    """
    if not istd_names:
        raise ValueError("At least one internal standard feature must be selected.")
    missing = [n for n in istd_names if n not in peak_df.index]
    if missing:
        raise ValueError(f"Internal standard(s) not found among features: {', '.join(missing)}")

    if len(istd_names) == 1:
        return istd_normalize(peak_df, istd_names[0]), []

    istd_rows = peak_df.loc[istd_names]
    remaining = peak_df.drop(index=istd_names)

    # Pooled fallback ISTD: per-sample geometric mean across all selected ISTDs,
    # used for any feature that can't be matched to a class-specific ISTD.
    pooled = np.exp(np.log(istd_rows.replace(0, np.nan)).mean(axis=0))

    feature_class = {}
    istd_class = {}
    if row_annotations is not None and "Class" in row_annotations.columns:
        for feat in remaining.index:
            if feat in row_annotations.index:
                feature_class[feat] = row_annotations.loc[feat, "Class"]
        for istd in istd_names:
            if istd in row_annotations.index:
                istd_class[istd] = row_annotations.loc[istd, "Class"]

    normalized_rows = []
    unmatched = []
    for feat in remaining.index:
        matched_istd = None
        feat_cls = feature_class.get(feat)
        if feat_cls is not None:
            for istd, cls in istd_class.items():
                if cls == feat_cls:
                    matched_istd = istd
                    break
        if matched_istd is not None:
            denom = istd_rows.loc[matched_istd].replace(0, np.nan)
        else:
            denom = pooled
            unmatched.append(feat)
        normalized_rows.append(remaining.loc[feat].div(denom))

    normalized = pd.DataFrame(normalized_rows, index=remaining.index, columns=remaining.columns)
    return normalized, unmatched


# ---------------------------------------------------------------------------
# 2. IQR normalization
# ---------------------------------------------------------------------------
def iqr_normalize(
    df: pd.DataFrame,
    axis: str = "sample",
    batch_map: pd.Series = None,
    avoid_nan: bool = False,
    stats_source: pd.DataFrame = None
) -> pd.DataFrame:
    """
    Median-IQR normalization (robust scaling):

        X_norm = (X - Median(X)) / IQR(X)

    Default (axis='sample'), indexed by metabolite i and sample j:

        normalized[i, j] = (X[i, j] - median_j) / IQR_j

    where median_j and IQR_j are computed from sample j's values across all
    metabolites i -- correcting for total-intensity/loading differences
    between samples. This is the recommended default for the app's
    Untargeted Metabolomics/Lipidomics pipeline, applied to the strict
    Log2-transformed data (see stats_analysis.strict_log2 / strict_log2 is
    applied before this function, not by it): normalized[i, j] =
    (log2(raw[i, j]) - median_j) / IQR_j.

    Parameters
    ----------
    df : pd.DataFrame
        Numeric feature x sample dataframe.

    axis : str
        'sample' (default):
            Normalize each sample across features:
            normalized[i, j] = (X[i, j] - median_j) / IQR_j.

        'feature':
            Normalize each feature across samples:
            normalized[i, j] = (X[i, j] - median_i) / IQR_i.

        'batch':
            Normalize each feature independently within each batch.
            Requires batch_map.

    batch_map : pd.Series, optional
        Series indexed by sample name containing batch labels.

    avoid_nan : bool, default False
        When a feature/sample/batch-group has zero spread (IQR = 0, i.e.
        every value is identical), the default behavior (avoid_nan=False,
        unchanged from prior releases) divides by NaN, propagating NaN to
        every value in that group. When avoid_nan=True, a zero IQR is
        instead treated as 1 (no scaling), so a zero-spread group is left
        centered at 0 rather than converted to missing. This does not
        change any value for groups that already have nonzero spread; it
        only affects the degenerate zero-IQR edge case, and never
        introduces NaN that wasn't already present in the input.

    stats_source : pd.DataFrame, optional
        When given, the per-sample (axis='sample') or per-feature
        (axis='feature') median and IQR are computed from `stats_source`
        instead of from `df` itself, and then applied to `df`. `df` is
        only ever the dataframe that gets transformed and returned --
        `stats_source` never appears in the output.

        This is for the case where `df` is a reduced dataframe (e.g. after
        an optional CV-based feature filter removed some rows), but the
        per-sample scaling statistics should still reflect the complete
        detected-feature background at that pipeline stage (every feature
        present before the optional filter), matching the recommended
        practice of computing median_j/IQR_j from the full feature table
        rather than a smaller, curated/exported subset. Concretely:

            normalized[i, j] = (df[i, j] - median_j(stats_source)) / IQR_j(stats_source)

        For axis='sample', `stats_source` must share the same columns
        (samples) as `df`; its rows (features) may differ or be a
        superset. For axis='feature', it must share the same index
        (features); its columns (samples) may differ or be a superset.
        Ignored when axis='batch'. Defaults to None, which reproduces the
        prior behavior of computing statistics from `df` itself.

    Returns
    -------
    pd.DataFrame
        IQR-normalized dataframe.

    Notes
    -----
    This is a centering/scaling transformation and can produce negative
    values. Therefore, the result should NOT be passed directly to the
    strict log2 transformation unless all values are positive.
    """

    def _safe_iqr(q1, q3):
        iqr = q3 - q1
        return iqr.replace(0, 1) if avoid_nan else iqr.replace(0, np.nan)

    if axis == "feature":

        src = stats_source if stats_source is not None else df

        median = src.median(axis=1)
        q1 = src.quantile(0.25, axis=1)
        q3 = src.quantile(0.75, axis=1)

        iqr = _safe_iqr(q1, q3)

        # Statistics are computed from `src` (features x its own samples) but
        # applied to `df`, aligned by feature (row) index -- df's rows are the
        # ones actually transformed/returned.
        return (
            df
            .sub(median.reindex(df.index), axis=0)
            .div(iqr.reindex(df.index), axis=0)
        )

    elif axis == "sample":

        src = stats_source if stats_source is not None else df

        median = src.median(axis=0)
        q1 = src.quantile(0.25, axis=0)
        q3 = src.quantile(0.75, axis=0)

        iqr = _safe_iqr(q1, q3)

        # Statistics are computed from `src` (all features detected for each
        # sample column, e.g. before an optional CV-based feature filter) but
        # applied to `df` (the, possibly filtered, feature set actually
        # normalized/returned), aligned by sample (column).
        return (
            df
            .sub(median.reindex(df.columns), axis=1)
            .div(iqr.reindex(df.columns), axis=1)
        )

    elif axis == "batch":

        if batch_map is None:
            raise ValueError(
                "batch_map (sample -> batch label) is required "
                "for batch-specific normalization."
            )

        out = df.copy()

        for batch in batch_map.dropna().unique():

            cols = batch_map.index[
                batch_map == batch
            ].tolist()

            # Keep only columns that actually exist in the dataframe.
            cols = [c for c in cols if c in df.columns]

            if not cols:
                continue

            sub = df[cols]

            median = sub.median(axis=1)
            q1 = sub.quantile(0.25, axis=1)
            q3 = sub.quantile(0.75, axis=1)

            iqr = _safe_iqr(q1, q3)

            out[cols] = (
                sub
                .sub(median, axis=0)
                .div(iqr, axis=0)
            )

        return out

    else:
        raise ValueError(
            "axis must be one of 'feature', 'sample', or 'batch'"
        )


# ---------------------------------------------------------------------------
# 3. STRICT Log2 transformation
# ---------------------------------------------------------------------------
def log2_transform(df: pd.DataFrame) -> pd.DataFrame:
    """
    Strict Log2 transformation.

        transformed = log2(x)

    IMPORTANT
    ---------
    No pseudocount is added.

    No constant is added.

    No zero replacement is performed.

    No shifting is performed.

    No arbitrary offset is applied.

    Values <= 0 cannot be log2 transformed and are converted to NaN.

    NaN values remain NaN.

    Parameters
    ----------
    df : pd.DataFrame
        Numeric dataframe.

    Returns
    -------
    pd.DataFrame
        Strict log2-transformed dataframe.
    """

    work = df.copy()

    # Convert values <= 0 to NaN.
    work = work.where(work > 0, np.nan)

    # Strict mathematical log2(x).
    transformed = np.log2(work)

    return transformed


# ---------------------------------------------------------------------------
# 4. Distribution plots
# ---------------------------------------------------------------------------
def distribution_plots(
    before: pd.DataFrame,
    after: pd.DataFrame,
    sample_id: str = None
):
    """
    Generate before/after distribution and box plots.

    Before:
        Raw peak-area values are displayed using a logarithmic x/y scale
        where appropriate.

    After:
        Strict log2-transformed values are displayed on a linear scale.

    Values that are <= 0 before transformation are excluded from the
    transformed distribution because they become NaN under strict log2.
    """

    # ------------------------------------------------------------------
    # Before data
    # ------------------------------------------------------------------
    b = before.values.flatten()

    b = b[
        np.isfinite(b) &
        (b > 0)
    ]

    # ------------------------------------------------------------------
    # After data
    # ------------------------------------------------------------------
    a = after.values.flatten()

    a = a[
        np.isfinite(a)
    ]

    # Single row of 4 panels (Before density, Before box, After density, After
    # box) instead of a 2x2 block -- a compact 4x1 strip that reads at a glance
    # and takes up far less vertical space than the old 2x2 layout. Displayed
    # at full tab width (see render_single_figure's width_ratio=None), so the
    # figure is sized to fill that width well rather than looking small with
    # empty space on either side.
    fig, axes = plt.subplots(
        1,
        4,
        figsize=(15, 3.4)
    )
    fs = 9.5

    # ------------------------------------------------------------------
    # Before: Distribution
    # ------------------------------------------------------------------
    if b.size:
        axes[0].hist(
            b,
            bins=60,
            alpha=0.8
        )

        axes[0].set_xscale("log")

    axes[0].set_title(
        "Before: Raw Peak Area\nDistribution",
        fontsize=fs, fontweight="bold", loc="center"
    )

    axes[0].set_xlabel(
        "Peak Area (log scale)", fontsize=fs
    )

    axes[0].set_ylabel(
        "Count", fontsize=fs
    )

    # ------------------------------------------------------------------
    # Before: Box plot
    # ------------------------------------------------------------------
    if b.size:

        try:
            axes[1].boxplot(
                [b],
                tick_labels=["Before"]
            )
        except TypeError:
            # Compatibility with older matplotlib versions.
            axes[1].boxplot(
                [b],
                labels=["Before"]
            )

        axes[1].set_yscale("log")

    axes[1].set_title(
        "Before: Box Plot",
        fontsize=fs, fontweight="bold", loc="center"
    )

    axes[1].set_ylabel(
        "Peak Area (log scale)", fontsize=fs
    )

    # ------------------------------------------------------------------
    # After: Strict Log2 distribution
    # ------------------------------------------------------------------
    if a.size:

        axes[2].hist(
            a,
            bins=60,
            alpha=0.8
        )

    axes[2].set_title(
        "After: Strict Log2\nDistribution",
        fontsize=fs, fontweight="bold", loc="center"
    )

    axes[2].set_xlabel(
        "log2(Peak Area)", fontsize=fs
    )

    axes[2].set_ylabel(
        "Count", fontsize=fs
    )

    # ------------------------------------------------------------------
    # After: Box plot
    # ------------------------------------------------------------------
    if a.size:

        try:
            axes[3].boxplot(
                [a],
                tick_labels=["After"]
            )
        except TypeError:
            # Compatibility with older matplotlib versions.
            axes[3].boxplot(
                [a],
                labels=["After"]
            )

    axes[3].set_title(
        "After: Strict Log2\nBox Plot",
        fontsize=fs, fontweight="bold", loc="center"
    )

    axes[3].set_ylabel(
        "log2(Peak Area)", fontsize=fs
    )

    for ax in axes:
        ax.tick_params(labelsize=fs - 1)

    # ------------------------------------------------------------------
    # Optional sample identifier
    # ------------------------------------------------------------------
    if sample_id:
        fig.suptitle(
            f"Normalization / Log2 Transformation: {sample_id}",
            fontsize=13, fontweight="bold"
        )

    fig.tight_layout()

    return fig
