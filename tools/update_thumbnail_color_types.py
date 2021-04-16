#!/usr/bin/env python

import time
import math
import sys
import datetime
import requests

import arrow

import tools.istarmap  # noqa: F401
import multiprocessing as mp
from tqdm import tqdm

from baselayer.app.env import load_env, parser


def test_connection(admin_token):

    RETRIES = 6
    timeout = 1
    conn = None
    for i in range(RETRIES):
        try:
            print(f"Connecting to database {cfg['database']['database']}")
            conn = init_db(**cfg['database'])
        except TimeoutError:
            if i == RETRIES - 1:
                print('FAIL')
                print()
                print(
                    f'Error: Could not connect to SkyPortal database; trying again in {timeout}s'
                )
                sys.exit(-1)
            else:
                time.sleep(timeout)
                timeout = max(timeout * 2, 30)
                print('Retrying connection...')

    return conn


def save_batch_using_copy(rows):
    import subprocess

    # Build command
    cmd = [
        "psql",
        "-c",
        "CREATE TEMP TABLE tmp_thumbnails (id int, is_grayscale bool, modified timestamp without time zone);",
        "-c",
        "\COPY tmp_thumbnails(id, is_grayscale, modified) FROM STDIN;",  # noqa: W605
        "-c",
        "UPDATE thumbnails SET (is_grayscale, modified) = (tmp_thumbnails.is_grayscale, tmp_thumbnails.modified) FROM tmp_thumbnails WHERE thumbnails.id = tmp_thumbnails.id;",
        "-c",
        "DROP TABLE tmp_thumbnails;",
        "-d",
        str(cfg["database.database"]),
        "-h",
        str(cfg["database.host"]),
        "-p",
        str(cfg["database.port"]),
        '-U',
        str(cfg["database.user"]),
    ]

    if cfg["database.password"] is not None:
        env = {"PGPASSWORD": cfg['database']['password']}
    else:
        cmd.append("--no-password")
        env = None

    cmd += [
        "--single-transaction",
        "--set=ON_ERROR_STOP=true",
    ]

    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    error_message = ""
    for row in rows:
        p.stdin.write(row.encode())

    p.stdin.close()
    p.wait()

    if p.returncode:
        error_message = p.stderr.readline().decode()

    return p.returncode, error_message


def process_batch(end_date, batch_no, batch_size, verbose=True):
    from skyportal.models import init_db, DBSession, Thumbnail
    from skyportal.utils.thumbnail import image_is_grayscale

    init_db(**cfg["database"])

    # Fetch batch of thumbnails
    thumbnails = (
        DBSession()
        .query(Thumbnail)
        .filter(Thumbnail.created_at < end_date)
        .limit(batch_size)
        .offset(batch_no * batch_size)
    )
    if verbose:
        print(f"Retrieved thumbnails: {[t.id for t in thumbnails]}")

    # Precompute the updated is_grayscale values for the batch
    batch_records = []
    for thumbnail in thumbnails:
        is_grayscale = None
        if thumbnail.file_uri is not None:
            is_grayscale = image_is_grayscale(thumbnail.file_uri)
        else:
            try:
                is_grayscale = image_is_grayscale(
                    requests.get(thumbnail.public_url, stream=True).raw
                )
            except requests.exceptions.RequestException:
                pass

        if verbose:
            print(f"Thumbnail {thumbnail.id} is {is_grayscale}")

        if is_grayscale is not None:
            utcnow = datetime.datetime.utcnow().isoformat()
            batch_records.append(f"{thumbnail.id}\t{is_grayscale}\t{utcnow}\n")

    # Update the compiled batch of rows in bulk
    return_code, msg = save_batch_using_copy(batch_records)
    if verbose:
        print(f"Output for batch {batch_no}: ", return_code, msg)


if __name__ == "__main__":
    parser.description = (
        "Iterate through SkyPortal thumbnails and update their color types"
    )
    parser.add_argument(
        "--end_date",
        help="Update thumbnails inserted before this datetime",
    )

    env, cfg = load_env()

    from skyportal.models import init_db, DBSession, Thumbnail

    conn = test_connection(cfg)

    end_date = arrow.get(env.end_date.strip()).datetime.replace(tzinfo=None)
    print(f"Retrieving thumbnail IDs created before {end_date} to update...")
    num_thumbnails = (
        DBSession().query(Thumbnail.id).filter(Thumbnail.created_at < end_date).count()
    )
    # Close the parent connection so each child can open their own
    DBSession().close()
    conn.dispose()

    print(DBSession().__dict__)

    print(f"Processing {num_thumbnails} thumbnails:")

    batch_size = 1
    num_batches = math.ceil(num_thumbnails / batch_size)
    input_list = [[end_date, i, batch_size, True] for i in range(num_batches)]
    with mp.Pool(processes=2) as p:
        for _ in tqdm(p.istarmap(process_batch, input_list), total=len(input_list)):
            pass
