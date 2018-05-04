import seaborn as sns
import matplotlib.pyplot as plt
import matplotlib as mpl
from matplotlib import transforms
from matplotlib.font_manager import FontProperties
from matplotlib.patches import PathPatch
import matplotlib.patheffects
from matplotlib.text import TextPath
import numpy as np
from selene.sequences import Genome


class TextPathRenderingEffect(matplotlib.patheffects.AbstractPathEffect):
    """
    This is a class for re-rendering text paths and preserving their scale.
    """
    def __init__(self, bar, x_translation=0., y_translation=0., x_scale=1., y_scale=1.):
        """

        Parameters
        ----------
        bar : matplotlib.patches.Patch
            The patch where the letter is.
        x_translation : float
            Amount by which to translate the x coordinate.
        y_translation : float
            Amount by which to translate the y coordinate.
        x_scale : float
            Amount by which to scale the width.
        y_scale : float
            Amount by which to scale the height.
        """
        self._bar = bar
        self._x_translation = x_translation
        self._y_translation = y_translation
        self._x_scale = x_scale
        self._y_scale = y_scale

    def draw_path(self, renderer, gc, tpath, affine, rgbFace=None):
        """
        Redraws the path.
        """
        b_x, b_y, b_w, b_h = self._bar.get_extents().bounds
        t_x, t_y, t_w, t_h = tpath.get_extents().bounds
        translation = [b_x - t_x, b_y - t_y]
        translation[0] += self._x_translation
        translation[1] += self._y_translation
        scale = [b_w / t_w, b_h / t_h]
        scale[0] *= self._x_scale
        scale[1] *= self._y_scale
        affine = affine.identity().scale(*scale).translate(*translation)
        renderer.draw_path(gc, tpath, affine, rgbFace)


def sequence_logo(scores, sequence_type=Genome, font_family="sans", font_size=180, width=1.0,
                  font_properties=None, ax=None, **kwargs):
    """

    Parameters
    ----------
    scores : np.ndarray
        A Lx|bases| matrix containing the scores for each position.
    sequence_type : class
        The type of sequence that the ISM results are associated with.
    font_family : str
        The font family to use.
    font_size : int
        The size of the font to use.
    width : float
        The size width of each character.
    font_properties : matplotlib.font_manager.FontProperties
        A FontProperties object specifying the properties of the font used.
    ax : matplotlib.pyplot.Axes
        An axes to plot on.
    color_scheme: list
        A list containing the colors to use, appearing in the order of the bases of the sequence type.

    Returns
    -------
    matplotlib.pyplot.Axes
        An axis containing the sequence logo plot.

    """
    scores = scores.transpose()

    if "colors" in kwargs:
        color_scheme = kwargs.pop("colors")
    else:
        color_scheme = ["orange", "red", "blue", "darkgreen"]
    if len(color_scheme) < len(sequence_type.BASES_ARR):
        raise ValueError("Color scheme is shorter than number of bases in sequence.")

    if scores.shape[0] != len(sequence_type.BASES_ARR):
        raise ValueError(f"Got score with {scores.shape[0]} bases for sequence"
                         f"with {len(sequence_type.BASES_ARR)} bases.")

    scores = np.flip(scores, axis=0)
    mpl.rcParams["font.family"] = font_family
    if font_properties is None:
        font_properties = FontProperties(size=font_size, weight="black")
    if ax is None:
        _, ax = plt.subplots(figsize=scores.shape)

    # Create stacked barplot, stacking after each base.
    last_positive_offset = np.zeros(scores.shape[1])
    last_negative_offset = np.zeros(scores.shape[1])
    for base_idx in range(scores.shape[0]):
        base = sequence_type.BASES_ARR[base_idx]
        x_coords = np.arange(scores.shape[1]) + 0.5
        y_coords = scores[base_idx, :]

        # Manage negatives and positives separately.
        offset = np.zeros_like(y_coords)
        negative_locs = y_coords < 0
        offset[negative_locs] = last_negative_offset[negative_locs]
        last_negative_offset[negative_locs] += y_coords[negative_locs]
        positive_locs = y_coords >= 0
        offset[positive_locs] = last_positive_offset[positive_locs]
        last_positive_offset[positive_locs] += y_coords[positive_locs]
        ax.bar(x_coords, y_coords, color=color_scheme[sequence_type.BASE_TO_INDEX[base]], width=width, bottom=offset)

    # Iterate over the barplot's bars and turn them into letters.
    new_patches = []
    for i, bar in enumerate(ax.patches):
        base_idx = i // scores.shape[1]
        # We construct a text path that tracks the bars in the barplot.
        # Thus, the barplot takes care of scaling and translation, and we just copy it.
        base = sequence_type.BASES_ARR[base_idx]
        text = TextPath((0., 0.), base, fontproperties=font_properties)
        b_x, b_y, b_w, b_h = bar.get_extents().bounds
        t_x, t_y, t_w, t_h = text.get_extents().bounds
        scale = (b_w / t_w, b_h / t_h)
        translation = (b_x - t_x, b_y - t_y)
        text = PathPatch(text, facecolor=bar.get_facecolor(), lw=0.)
        bar.set_facecolor("none")
        text.set_path_effects([TextPathRenderingEffect(bar)])  # This redraws the letters on resize.
        transform = transforms.Affine2D().translate(*translation).scale(*scale)
        text.set_transform(transform)
        # axis.add_artist(text)
        new_patches.append(text)

    for patch in new_patches:
        ax.add_patch(patch)
    ax.set_xlim(0, scores.shape[1])
    return ax


def rescale_feature_matrix(scores, base_scaling="identity", position_scaling="identity"):
    """
    Performs base-wise and position-wise scaling of a feature matrix.

    Parameters
    ----------
    scores : numpy.ndarray
        A Lx|bases| matrix containing the scores for each position.
    base_scaling : str
        The type of scaling performed on each base at a given position.
            identity: No transformation will be applied to the data.
            probability : The relative sizes of the bases will be the original input probabilities.
            max_effect : The relative sizes of the bases will be the max effect of the original input values.
    position_scaling : str
        The type of scaling performed on each position.
            identity: No transformation will be applied to the data.
            probability: The sum of values at a position will be equal to the
                         sum of the original input values at that position.
            max_effect: The sum of values at a position will be equal to the
                        sum of the max effect values of the original input
                        values at that position.
    kwargs : dict
        Passed to plot_sequence_logo

    Returns
    -------
    numpy.ndarray :
        The transformed array.

    """
    scores = scores.transpose()
    rescaled_scores = scores

    # Scale individual bases.
    if base_scaling == "identity":
        pass
    elif base_scaling == "max_effect":
        rescaled_scores = scores - np.min(scores, axis=0)
    elif base_scaling == "probability":
        pass
    else:
        raise ValueError(f"Could not find base scaling \"{base_scaling}\".")

    # Scale each position
    if position_scaling == "identity":
        pass
    elif position_scaling == "max_effect":
        max_effects = np.max(scores, axis=0) - np.min(scores, axis=0)
        rescaled_scores /= max_effects
    elif position_scaling == "probability":
        rescaled_scores /= np.sum(scores, axis=0)
    else:
        raise ValueError(f"Could not find position scaling \"{position_scaling}\".")
    return rescaled_scores.transpose()


def heatmap(scores, sequence_type=Genome, mask=None, **kwargs):
    """
    Plots scores on a heatmap.

    Parameters
    ----------
    scores : numpy.ndarray
        A Lx|bases| matrix containing the scores for each position.
    sequence_type : class
        The type of sequence that the ISM results are associated with.
    mask : numpy.ndarray, None
        A Lx|bases| matrix containing 1s at positions to mask.
    kwargs : dict
        Keyword arguments to pass to seaborn.heatmap().
        Some useful ones are:
            cbar_kws: Change keyword arguments to the colorbar.
            yticklabels: Manipulate the tick labels on the y axis.
            cbar: If False, hide the colorbar, otherwise show the colorbar.
            cmap: The color map to use for the heatmap.

    Returns
    -------
    matplotlib.pytplot.Axes
        An axis containing the heatmap plot.

    """

    if mask is not None:
        mask = mask.transpose()
    scores = scores.transpose()
    if "yticklabels" in kwargs:
        yticklabels = kwargs.pop("yticklabels")
    else:
        yticklabels = sequence_type.BASES_ARR[::-1]
    if "cbar_kws" in kwargs:
        cbar_kws = kwargs.pop("cbar_kws")
    else:
        cbar_kws = dict(use_gridspec=False, location="bottom", pad=0.2)
    if "cmap" in kwargs:
        cmap = kwargs.pop("cmap")
    else:
        cmap = "Blues_r"
    return sns.heatmap(scores, mask=mask, yticklabels=yticklabels, cbar_kws=cbar_kws, cmap=cmap, **kwargs)


    #return _plot_sequence_logo(scores=rescaled_scores.transpose(), sequence_type=sequence_type, **kwargs)
