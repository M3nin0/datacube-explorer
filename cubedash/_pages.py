import functools
import inspect
import itertools
import sys
import time
from datetime import datetime
from typing import Tuple

import flask
import structlog
from flask import abort, redirect, request, url_for
from sqlalchemy import event
from werkzeug.datastructures import MultiDict

import cubedash
import datacube
from cubedash.summary import RegionInfo, TimePeriodOverview
from cubedash.summary._stores import ProductSummary
from datacube.model import DatasetType
from datacube.scripts.dataset import build_dataset_info

from . import _api, _dataset, _filters, _model, _platform, _product, _reports
from . import _utils as utils
from ._utils import alchemy_engine, as_json

app = _model.app
app.register_blueprint(_filters.bp)
app.register_blueprint(_api.bp)
app.register_blueprint(_dataset.bp)
app.register_blueprint(_product.bp)
app.register_blueprint(_platform.bp)
app.register_blueprint(_reports.bp)

_LOG = structlog.getLogger()

_HARD_SEARCH_LIMIT = app.config.get("CUBEDASH_HARD_SEARCH_LIMIT", 500)


# @app.route('/')
@app.route("/<product_name>")
@app.route("/<product_name>/<int:year>")
@app.route("/<product_name>/<int:year>/<int:month>")
@app.route("/<product_name>/<int:year>/<int:month>/<int:day>")
def overview_page(
    product_name: str = None, year: int = None, month: int = None, day: int = None
):
    product, product_summary, selected_summary = _load_product(
        product_name, year, month, day
    )

    return flask.render_template(
        "overview.html",
        year=year,
        month=month,
        day=day,
        # Which data to preload with the page?
        regions_geojson=_model.get_regions_geojson(product_name, year, month, day),
        datasets_geojson=None,  # _model.get_datasets_geojson(product_name, year, month, day),
        footprint_geojson=_model.get_footprint_geojson(product_name, year, month, day),
        product=product,
        product_region_info=RegionInfo.for_product(product),
        # Summary for the whole product
        product_summary=product_summary,
        # Summary for the users' currently selected filters.
        selected_summary=selected_summary,
    )


# @app.route('/datasets')
@app.route("/datasets/<product_name>")
@app.route("/datasets/<product_name>/<int:year>")
@app.route("/datasets/<product_name>/<int:year>/<int:month>")
@app.route("/datasets/<product_name>/<int:year>/<int:month>/<int:day>")
def search_page(
    product_name: str = None, year: int = None, month: int = None, day: int = None
):
    product, product_summary, selected_summary = _load_product(
        product_name, year, month, day
    )
    time_range = utils.as_time_range(
        year, month, day, tzinfo=_model.STORE.grouping_timezone
    )

    args = MultiDict(flask.request.args)
    query = utils.query_to_search(args, product=product)

    # Always add time range, selected product to query
    if product_name:
        query["product"] = product_name
    if time_range:
        query["time"] = time_range

    _LOG.info("query", query=query)

    # TODO: Add sort option to index API
    datasets = sorted(
        _model.STORE.index.datasets.search(**query, limit=_HARD_SEARCH_LIMIT),
        key=lambda d: d.center_time,
    )

    if request_wants_json():
        return as_json(
            dict(datasets=[build_dataset_info(_model.STORE.index, d) for d in datasets])
        )
    return flask.render_template(
        "search.html",
        year=year,
        month=month,
        day=day,
        product=product,
        # Summary for the whole product
        product_summary=product_summary,
        # Summary for the users' currently selected filters.
        selected_summary=selected_summary,
        datasets=datasets,
        query_params=query,
        result_limit=_HARD_SEARCH_LIMIT,
    )


@app.route("/region/<product_name>/<region_code>")
@app.route("/region/<product_name>/<region_code>/<int:year>")
@app.route("/region/<product_name>/<region_code>/<int:year>/<int:month>")
@app.route("/region/<product_name>/<region_code>/<int:year>/<int:month>/<int:day>")
def region_page(
    product_name: str = None,
    region_code: str = None,
    year: int = None,
    month: int = None,
    day: int = None,
):
    product, product_summary, selected_summary = _load_product(
        product_name, year, month, day
    )

    datasets = list(
        _model.STORE.find_datasets_for_region(
            product_name, region_code, year, month, day, limit=_HARD_SEARCH_LIMIT
        )
    )

    if len(datasets) == 1 and "feelinglucky" in flask.request.args:
        return flask.redirect(url_for("dataset.dataset_page", id_=datasets[0].id))

    if request_wants_json():
        return as_json(
            dict(datasets=[build_dataset_info(_model.STORE.index, d) for d in datasets])
        )
    return flask.render_template(
        "region.html",
        year=year,
        month=month,
        day=day,
        region_code=region_code,
        product=product,
        product_region_info=RegionInfo.for_product(product),
        # Summary for the whole product
        product_summary=product_summary,
        # Summary for the users' currently selected filters.
        selected_summary=selected_summary,
        datasets=datasets,
        result_limit=_HARD_SEARCH_LIMIT,
    )


@app.route("/<product_name>/spatial")
def spatial_page(product_name: str):
    """Legacy redirect to maintain old bookmarks"""
    return redirect(url_for("overview_page", product_name=product_name))


@app.route("/<product_name>/timeline")
def timeline_page(product_name: str):
    """Legacy redirect to maintain old bookmarks"""
    return redirect(url_for("overview_page", product_name=product_name))


def _load_product(
    product_name, year, month, day
) -> Tuple[DatasetType, ProductSummary, TimePeriodOverview]:
    product = None
    if product_name:
        try:
            product = _model.STORE.get_dataset_type(product_name)
        except KeyError:
            abort(404, "Unknown product %r" % product_name)

    product_summary = _model.get_product_summary(product_name)
    time_summary = _model.get_time_summary(product_name, year, month, day)
    return product, product_summary, time_summary


def request_wants_json():
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return (
        best == "application/json"
        and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]
    )


@app.route("/about")
def about_page():
    return flask.render_template("about.html")


@app.context_processor
def inject_globals():
    product_summaries = _model.get_products_with_summaries()

    # Group by product type
    def key(t):
        return t[0].fields.get("product_type")

    grouped_product_summarise = sorted(
        (
            (name or "", list(items))
            for (name, items) in itertools.groupby(
                sorted(product_summaries, key=key), key=key
            )
        ),
        # Show largest groups first
        key=lambda k: len(k[1]),
        reverse=True,
    )

    return dict(
        grouped_products=grouped_product_summarise,
        current_time=datetime.utcnow(),
        datacube_version=datacube.__version__,
        app_version=cubedash.__version__,
        last_updated_time=_model.get_last_updated(),
    )


@app.route("/")
def default_redirect():
    """Redirect to default starting page."""
    available_product_names = [p.name for p, _ in _model.get_products_with_summaries()]

    for product_name in _model.DEFAULT_START_PAGE_PRODUCTS:
        if product_name in available_product_names:
            default_product = product_name
            break
    else:
        default_product = available_product_names[0]

    return flask.redirect(flask.url_for("overview_page", product_name=default_product))


# Add server timings to http headers.
if app.debug or app.config.get("CUBEDASH_SHOW_PERF_TIMES", False):

    @app.before_request
    def time_start():
        flask.g.start_render = time.time()
        flask.g.datacube_query_time = 0
        flask.g.datacube_query_count = 0

    @event.listens_for(alchemy_engine(_model.STORE.index), "before_cursor_execute")
    def before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        conn.info.setdefault("query_start_time", []).append(time.time())

    @event.listens_for(alchemy_engine(_model.STORE.index), "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        flask.g.datacube_query_time += time.time() - conn.info["query_start_time"].pop(
            -1
        )
        flask.g.datacube_query_count += 1
        # print(f"===== {flask.g.datacube_query_time*1000} ===: {repr(statement)}")

    @app.after_request
    def time_end(response: flask.Response):
        render_time = time.time() - flask.g.start_render
        response.headers.add_header(
            "Server-Timing",
            f"app;dur={render_time*1000},"
            f'odcquery;dur={flask.g.datacube_query_time*1000};desc="ODC query time",'
            f"odcquerycount_{flask.g.datacube_query_count};"
            f'desc="{flask.g.datacube_query_count} ODC queries"',
        )
        return response

    def decorate_all_methods(cls, decorator):
        """
        Decorate all public methods of the class with the given decorator.
        """
        for name, clasification, clz, attr in inspect.classify_class_attrs(cls):
            if clasification == "method" and not name.startswith("_"):
                setattr(cls, name, decorator(attr))
        return cls

    def print_datacube_query_times():
        from click import style

        def with_timings(function):
            """
            Decorate the given function with a stderr print of timing
            """

            @functools.wraps(function)
            def decorator(*args, **kwargs):
                start_time = time.time()
                ret = function(*args, **kwargs)
                duration_secs = time.time() - start_time
                print(
                    f"== Index Call == {style(function.__name__, bold=True)}: "
                    f"{duration_secs*1000}",
                    file=sys.stderr,
                    flush=True,
                )
                return ret

            return decorator

        # Print call time for all db layer calls.
        import datacube.drivers.postgres._api as api

        decorate_all_methods(api.PostgresDbAPI, with_timings)

    print_datacube_query_times()
