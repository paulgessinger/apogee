#!/bin/bash

set -e

flask db upgrade
exec flask run
