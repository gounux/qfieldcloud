#!/usr/bin/env python3

import argparse
import hashlib
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path, PurePath
from typing import Dict, List

import boto3
import mock
import qfieldcloud.qgis.apply_deltas
import qfieldcloud.qgis.process_projectfile
from libqfieldsync.offline_converter import ExportType, OfflineConverter
from libqfieldsync.project import ProjectConfiguration
from qfieldcloud.qgis.utils import Step
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsOfflineEditing,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgisInterface, QgsMapCanvas
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtWidgets import QMainWindow

# Get environment variables
STORAGE_ACCESS_KEY_ID = os.environ.get("STORAGE_ACCESS_KEY_ID")
STORAGE_SECRET_ACCESS_KEY = os.environ.get("STORAGE_SECRET_ACCESS_KEY")
STORAGE_BUCKET_NAME = os.environ.get("STORAGE_BUCKET_NAME")
STORAGE_REGION_NAME = os.environ.get("STORAGE_REGION_NAME")
STORAGE_ENDPOINT_URL = os.environ.get("STORAGE_ENDPOINT_URL")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _write_log_message(message, tag, level):
    time = datetime.now().isoformat()
    level_str = "UNKNOWN"

    if level == Qgis.Info:
        level_str = "INFO"
    elif level == Qgis.Warning:
        level_str = "WARNING"
    else:
        level_str = "CRITICAL"

    logger.info(f"QMessageLog {time} {level_str} {tag} {message}")


def _get_s3_resource():
    """Get S3 Service Resource object"""
    # Create session
    session = boto3.Session(
        aws_access_key_id=STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=STORAGE_SECRET_ACCESS_KEY,
        region_name=STORAGE_REGION_NAME,
    )

    return session.resource("s3", endpoint_url=STORAGE_ENDPOINT_URL)


def _get_s3_bucket():
    """Get S3 Bucket according to the env variable
    STORAGE_BUCKET_NAME"""

    s3 = _get_s3_resource()
    bucket = s3.Bucket(STORAGE_BUCKET_NAME)
    return bucket


def _get_sha256sum(filepath):
    """Calculate sha256sum of a file"""
    BLOCKSIZE = 65536
    hasher = hashlib.sha256()
    with filepath as f:
        buf = f.read(BLOCKSIZE)
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(BLOCKSIZE)
    return hasher.hexdigest()


def _download_project_directory(project_id: str, tmpdir: Path = None) -> Path:
    """Download the files in the project "working" directory from the S3
    Storage into a temporary directory. Returns the directory path"""

    bucket = _get_s3_bucket()

    # Prefix of the working directory on the Storages
    working_prefix = "/".join(["projects", project_id, "files"])

    if not tmpdir:
        # Create a temporary directory
        tmpdir = Path(tempfile.mkdtemp())

    # Create a local working directory
    working_dir = tmpdir.joinpath("files")
    working_dir.mkdir(parents=True)

    # Download the files
    for obj in bucket.objects.filter(Prefix=working_prefix):
        key_filename = PurePath(obj.key)

        # Get the path of the file relative to the project directory
        relative_filename = key_filename.relative_to(*key_filename.parts[:2])
        absolute_filename = tmpdir.joinpath(relative_filename)
        absolute_filename.parent.mkdir(parents=True, exist_ok=True)

        bucket.download_file(obj.key, str(absolute_filename))

    return tmpdir


def _upload_project_directory(projectid, local_dir):
    """Upload the files in the local_dir (export directory) to the
    storage"""

    bucket = _get_s3_bucket()

    export_prefix = "/".join(["projects", projectid, "export"])

    # Remove existing export directory on the storage
    bucket.objects.filter(Prefix=export_prefix).delete()

    # Loop recursively in the local export directory
    for elem in Path(local_dir).rglob("*.*"):
        # Don't upload .qgs~ and .qgz~ files
        if str(elem).endswith("~"):
            continue
        # Calculate sha256sum
        with open(elem, "rb") as e:
            sha256sum = _get_sha256sum(e)

        # Create the key
        key = "/".join([export_prefix, str(elem.relative_to(*elem.parts[:4]))])
        metadata = {"sha256sum": sha256sum}
        bucket.upload_file(str(elem), key, ExtraArgs={"Metadata": metadata})


def _upload_delta_modified_files(projectid, local_dir):
    """Upload the files changed by apply delta to the files/ directory of
    the storage, qgs project excluded"""

    bucket = _get_s3_bucket()

    files_prefix = "/".join(["projects", projectid, "files"])

    # Loop recursively in the local files directory
    for elem in Path(local_dir).rglob("*.*"):
        # Don't upload .qgs~ and .qgz~ files
        if str(elem).endswith("~"):
            continue
        if str(elem).endswith("qfieldcloudbackup"):
            continue

        # Calculate sha256sum
        with open(elem, "rb") as e:
            sha256sum = _get_sha256sum(e)

        # Create the key
        key = "/".join([files_prefix, str(elem.relative_to(*elem.parts[:4]))])
        metadata = {"sha256sum": sha256sum}

        # Check if the file is different on the storage
        storage_metadata = bucket.Object(key).metadata
        # depends on the storage
        storage_metadata["Sha256sum"] = storage_metadata.get(
            "Sha256sum", storage_metadata.get("sha256sum", None)
        )
        if storage_metadata["Sha256sum"] != sha256sum:
            bucket.upload_file(str(elem), key, ExtraArgs={"Metadata": metadata})


def _call_qfieldsync_exporter(project_filepath: Path, export_dir: Path) -> Dict:
    """Call the function of QFieldSync to export a project for QField"""

    argvb = list(map(os.fsencode, [""]))
    qgis_app = QgsApplication(argvb, True)
    qgis_app.initQgis()

    iface = mock.Mock(spec=QgisInterface)
    iface.mainWindow.return_value = QMainWindow()

    canvas = QgsMapCanvas(iface.mainWindow())
    canvas.resize(QSize(400, 400))

    # Set the canvas object name to the
    # exported project because qfieldsync running
    # headless on the server doesn't do that
    canvas.setObjectName("theMapCanvas")

    project = QgsProject.instance()
    if not project_filepath.exists():
        raise FileNotFoundError(project_filepath)

    if not project.read(str(project_filepath)):
        raise Exception(f"Unable to open file with QGIS: {project_filepath}")

    # Set the crs from the original project to the canvas
    # so it will be present in the exported project
    canvas.setDestinationCrs(project.crs())

    layers = project.mapLayers()
    # Check if the layers are valid (i.e. if the datasources are available)
    layer_checks = {}
    for layer in layers.values():
        is_valid = True
        status = "ok"
        if layer:
            if layer.dataProvider():
                if not layer.dataProvider().isValid():
                    is_valid = False
                    status = "invalid_dataprovider"
                # there might be another reason why the layer is not valid, other than the data provider
                elif not layer.isValid():
                    is_valid = False
                    status = "invalid_layer"
            else:
                is_valid = False
                status = "missing_dataprovider"
        else:
            is_valid = False
            status = "missing_layer"

        layer_checks[layer.id()] = {
            "name": layer.name(),
            "valid": is_valid,
            "status": status,
        }

    project_config = ProjectConfiguration(project)
    vl_extent = QgsRectangle()
    vl_extent_crs = project.crs().authid()

    if project_config.area_of_interest and project_config.area_of_interest_crs:
        vl_extent = project_config.area_of_interest
        vl_extent_crs = project_config.area_of_interest_crs
    else:
        for layer in layers.values():
            if type(layer) != QgsVectorLayer:
                continue

            transform = QgsCoordinateTransform(layer.crs(), project.crs(), project)
            vl_extent.combineExtentWith(transform.transformBoundingBox(layer.extent()))

        vl_extent_crs = project.crs().authid()

    offline_editing = QgsOfflineEditing()
    offline_converter = OfflineConverter(
        project,
        str(export_dir),
        vl_extent.asWktPolygon(),
        vl_extent_crs,
        offline_editing,
        export_type=ExportType.Cloud,
    )

    # Disable the basemap generation because it needs the processing
    # plugin to be installed
    offline_converter.project_configuration.create_base_map = False
    offline_converter.convert()

    qgis_app.exitQgis()

    return layer_checks


def cmd_export_project(args):
    project_id = args.projectid
    project_file = args.project_file

    tmpdir = Path(tempfile.mkdtemp())
    exportdir = tmpdir.joinpath("export")
    exportdir.mkdir()

    arguments = {
        "tmpdir": tmpdir,
        "project_id": project_id,
        "project_filename": tmpdir.joinpath("files", project_file),
        "exportdir": exportdir,
    }
    steps: List[Step] = [
        Step(
            name="Download Project Directory",
            arg_names=["project_id", "tmpdir"],
            method=_download_project_directory,
            return_names=["tmp_project_dir"],
            public_returns=["tmp_project_dir"],
        ),
        Step(
            name="Export Project",
            arg_names=["project_filename", "exportdir"],
            return_names=["layer_checks"],
            output_names=["layer_checks"],
            method=_call_qfieldsync_exporter,
        ),
        Step(
            name="Upload Exported Project",
            arg_names=["project_id", "exportdir"],
            method=_upload_project_directory,
        ),
    ]

    qfieldcloud.qgis.utils.perform_task(
        steps,
        arguments,
        Path("/io").joinpath("feedback.json"),
    )


def _apply_delta(args):
    projectid = args.projectid
    project_file = args.project_file

    tmpdir = _download_project_directory(projectid)
    deltafile = "/io/deltafile.json"

    project_filepath = os.path.join(tmpdir, "files", project_file)

    has_errors = qfieldcloud.qgis.apply_deltas.cmd_delta_apply(
        opts={
            "project": project_filepath,
            "delta_file": deltafile,
            "delta_log": "/io/deltalog.json",
            "inverse": False,
            "overwrite_conflicts": args.overwrite_conflicts,
            "transaction": False,
        }
    )

    _upload_delta_modified_files(projectid, os.path.join(tmpdir, "files"))

    exit(int(has_errors))


def cmd_process_projectfile(args):
    project_id = args.projectid
    project_file = args.project_file

    tmpdir = Path(tempfile.mkdtemp())
    arguments = {
        "tmpdir": tmpdir,
        "project_id": project_id,
        "project_filename": tmpdir.joinpath("files", project_file),
        "thumbnail_filename": Path("/io").joinpath("thumbnail.png"),
    }
    steps: List[Step] = [
        Step(
            name="Download Project Directory",
            arg_names=["project_id", "tmpdir"],
            method=_download_project_directory,
            return_names=["tmp_project_dir"],
            public_returns=["tmp_project_dir"],
        ),
        Step(
            name="Project Validity Check",
            arg_names=["project_filename"],
            method=qfieldcloud.qgis.process_projectfile.check_valid_project_file,
        ),
        Step(
            name="Opening Check",
            arg_names=["project_filename"],
            method=qfieldcloud.qgis.process_projectfile.load_project_file,
            return_names=["project"],
            public_returns=["project"],
        ),
        Step(
            name="Layer Validity Check",
            arg_names=["project"],
            method=qfieldcloud.qgis.process_projectfile.check_layer_validity,
            return_names=["layers_summary"],
            output_names=["layers_summary"],
        ),
        Step(
            name="Generate Thumbnail Image",
            arg_names=["project", "thumbnail_filename"],
            method=qfieldcloud.qgis.process_projectfile.generate_thumbnail,
        ),
    ]

    qfieldcloud.qgis.utils.perform_task(
        steps,
        arguments,
        Path("/io").joinpath("feedback.json"),
    )


if __name__ == "__main__":

    # Set S3 logging levels
    logging.getLogger("boto3").setLevel(logging.CRITICAL)
    logging.getLogger("botocore").setLevel(logging.CRITICAL)
    logging.getLogger("nose").setLevel(logging.CRITICAL)
    logging.getLogger("s3transfer").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)

    QgsApplication.messageLog().messageReceived.connect(_write_log_message)

    parser = argparse.ArgumentParser(prog="COMMAND")

    subparsers = parser.add_subparsers(dest="cmd")

    parser_export = subparsers.add_parser("export", help="Export a project")
    parser_export.add_argument("projectid", type=str, help="projectid")
    parser_export.add_argument("project_file", type=str, help="QGIS project file path")
    parser_export.set_defaults(func=cmd_export_project)

    parser_delta = subparsers.add_parser("apply-delta", help="Apply deltafile")
    parser_delta.add_argument("projectid", type=str, help="projectid")
    parser_delta.add_argument("project_file", type=str, help="QGIS project file path")
    parser_delta.add_argument(
        "--overwrite-conflicts", dest="overwrite_conflicts", action="store_true"
    )
    parser_delta.set_defaults(func=_apply_delta)

    parser_process_projectfile = subparsers.add_parser(
        "process-qgis-projectfile", help="Process QGIS project file"
    )
    parser_process_projectfile.add_argument("projectid", type=str, help="projectid")
    parser_process_projectfile.add_argument(
        "project_file", type=str, help="QGIS project file path"
    )
    parser_process_projectfile.set_defaults(func=cmd_process_projectfile)

    args = parser.parse_args()
    args.func(args)
