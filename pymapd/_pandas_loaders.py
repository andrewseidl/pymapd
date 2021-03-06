import six
import datetime

from pandas.api.types import (
    is_bool_dtype,
    is_integer_dtype,
    is_float_dtype,
    is_object_dtype,
    is_datetime64_any_dtype,
)

from mapd.ttypes import (
    TColumn, TColumnData, TColumnType, TTypeInfo, TDatumType
)
from ._utils import (
    date_to_seconds, time_to_seconds, datetime_to_seconds,
    mapd_to_na, mapd_to_slot
)


def get_mapd_dtype(data):
    "Get the mapd type"
    if is_object_dtype(data):
        return get_mapd_type_from_object(data)
    else:
        return get_mapd_type_from_known(data.dtype)


def get_mapd_type_from_known(dtype):
    """For cases where pandas type system matches"""
    if is_bool_dtype(dtype):
        return 'BOOL'
    elif is_integer_dtype(dtype):
        if dtype.itemsize <= 1:
            return 'TINYINT'
        elif dtype.itemsize == 2:
            return 'SMALLINT'
        elif dtype.itemsize == 4:
            return 'INT'
        else:
            return 'BIGINT'
    elif is_float_dtype(dtype):
        if dtype.itemsize <= 4:
            return 'FLOAT'
        else:
            return 'DOUBLE'
    elif is_datetime64_any_dtype(dtype):
        return 'TIMESTAMP'
    else:
        raise TypeError("Unhandled type {}".format(dtype))


def get_mapd_type_from_object(data):
    """For cases where the type system mismatches"""
    try:
        val = data.dropna().iloc[0]
    except IndexError:
        raise IndexError("Not any valid values to infer the type")
    if isinstance(val, six.string_types):
        return 'STR'
    elif isinstance(val, datetime.date):
        return 'DATE'
    elif isinstance(val, datetime.time):
        return 'TIME'
    elif isinstance(val, int):
        return 'INT'
    else:
        raise TypeError("Unhandled type {}".format(data.dtype))


def thrift_cast(data, mapd_type):
    """Cast data type to the expected thrift types"""
    import pandas as pd

    if mapd_type == 'TIMESTAMP':
        return datetime_to_seconds(data)
    elif mapd_type == 'TIME':
        return pd.Series(time_to_seconds(x) for x in data)
    elif mapd_type == 'DATE':
        return date_to_seconds(data)


def build_input_columnar(df, preserve_index=True):
    if preserve_index:
        df = df.reset_index()

    input_cols = []
    all_nulls = None

    for col in df.columns:
        data = df[col]
        mapd_type = get_mapd_dtype(data)
        has_nulls = data.hasnans

        if has_nulls:
            nulls = data.isnull().values
        elif all_nulls is None:
            nulls = all_nulls = [False] * len(df)

        if mapd_type in {'TIME', 'TIMESTAMP', 'DATE'}:
            # requires a cast to integer
            data = thrift_cast(data, mapd_type)

        if has_nulls:
            data = data.fillna(mapd_to_na[mapd_type])

            if mapd_type not in {'FLOAT', 'DOUBLE', 'VARCHAR', 'STR'}:
                data = data.astype('int64')
        # use .values so that indexes don't have to be serialized too
        kwargs = {mapd_to_slot[mapd_type]: data.values}

        input_cols.append(
            TColumn(data=TColumnData(**kwargs), nulls=nulls)
        )

    return input_cols


def _cast_int8(data):
    import pandas as pd
    if isinstance(data, pd.DataFrame):
        cols = data.select_dtypes(include=['i1']).columns
        data[cols] = data[cols].astype('i2')
    # TODO: Casts for pyarrow (waiting on python bindings for casting)
    # ARROW-229 did it for C++
    return data


def _serialize_arrow_payload(data, table_metadata, preserve_index=True):
    import pyarrow as pa
    import pandas as pd

    if isinstance(data, pd.DataFrame):
        data = _cast_int8(data)
        data = pa.RecordBatch.from_pandas(data, preserve_index=preserve_index)

    stream = pa.BufferOutputStream()
    writer = pa.RecordBatchStreamWriter(stream, data.schema)

    if isinstance(data, pa.RecordBatch):
        writer.write_batch(data)
    elif isinstance(data, pa.Table):
        writer.write_table(data)

    writer.close()
    return stream.getvalue()


def build_row_desc(data, preserve_index=False):
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("create_table requires pandas.")

    if not isinstance(data, pd.DataFrame):
        # Once https://issues.apache.org/jira/browse/ARROW-1576 is complete
        # we can support pa.Table here too
        raise TypeError("Create table is not supported for type {}. "
                        "Use a pandas DataFrame, or perform the create "
                        "separately".format(type(data)))

    if preserve_index:
        data = data.reset_index()
    dtypes = [(col, get_mapd_dtype(data[col])) for col in data.columns]
    # row_desc :: List<TColumnType>
    row_desc = [
        TColumnType(name, TTypeInfo(getattr(TDatumType, mapd_type)))
        for name, mapd_type in dtypes
    ]
    return row_desc
