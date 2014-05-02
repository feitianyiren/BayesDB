#
#   Copyright (c) 2010-2014, MIT Probabilistic Computing Project
#
#   Lead Developers: Jay Baxter and Dan Lovell
#   Authors: Jay Baxter, Dan Lovell, Baxter Eaves, Vikash Mansinghka
#   Research Leads: Vikash Mansinghka, Patrick Shafto
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import re
import utils
import numpy
import os
import pylab
import matplotlib.cm
import inspect
import operator
import ast
import string

import utils
import functions
import data_utils as du
from pyparsing import *
import bayesdb.bql_grammar as bql_grammar

def get_conditions_from_whereclause(whereclause, M_c, T, column_lists):## TODO Deprecate
  if whereclause == None:
    return ""
  ## Create conds: the list of conditions in the whereclause.
  ## List of (c_idx, op, val) tuples.
  conds = list() 
  operator_map = {'<=': operator.le, '<': operator.lt, '=': operator.eq, '>': operator.gt, '>=': operator.ge, 'in': operator.contains}
  
  top_level_parse = whereclause
  for inner_element in top_level_parse.where_conditions:
    if inner_element.confidence != '':
      confidence = inner_element.confidence
    else:
      confidence = None
    ## simple where column = value statement
    if inner_element.operation != '':
      op = operator_map[inner_element.operation]
    raw_val = inner_element.value
    if utils.is_int(raw_val):
      val = int(raw_val)
    elif utils.is_float(raw_val):
      val = float(raw_val)
    else:
      val = raw_val
    if inner_element.function.function_id == 'predictive probability of':
      if M_c['name_to_idx'].has_key(inner_element.function.column):
        column_index = M_c['name_to_idx'][inner_element.function.column]
        conds.append(((functions._predictive_probability,column_index), op, val))
        continue
    elif inner_element.function.function_id == 'typicality':
      conds.append(((functions._row_typicality, True), op, val))
      continue
    elif inner_element.function.function_id == 'similarity to':
      if inner_element.function.row_id == '':
        column_name = inner_element.function.column
        try:
          column_value = int(inner_element.function.column_value)
        except ValueError:
          try:
            column_value = float(inner_element.function.column_value)
          except ValueError:
            column_value = inner_element.function.column_value 
        column_index =  M_c['name_to_idx'][column_name]
        for row_id, T_row in enumerate(T):
          row_values = convert_row_from_codes_to_values(T_row, M_c)
          if row_values[column_index] == column_value:
            target_row_id = row_id
            break
      else: 
        target_row_id = int(inner_element.function.row_id)
      respect_to_clause = inner_element.function.with_respect_to
      target_column_ids = None
      if respect_to_clause != '':
        target_columns = respect_to_clause.column_list
        target_colnames = [colname.strip() for colname in utils.column_string_splitter(','.join(target_columns), M_c, column_lists)]
        utils.check_for_duplicate_columns(target_colnames)
        target_column_ids = [M_c['name_to_idx'][colname] for colname in target_colnames]
      conds.append(((functions._similarity, (target_row_id, target_column_ids)), op, val))
      continue
    elif inner_element.function.function_id == "key in":
      val = inner_element.function.row_list
      op = operator_map['in']
      conds.append(((functions._row_id, None), op, val))
      continue
    elif inner_element.function.column != '':
      colname = inner_element.function.column
      if M_c['name_to_idx'].has_key(colname.lower()):
        if utils.get_cctype_from_M_c(M_c, colname.lower()):
          val = str(val)## TODO hack, fix with util
        conds.append(((functions._column, M_c['name_to_idx'][colname.lower()]), op, val))
        continue
      raise utils.BayesDBParseError("Invalid where clause argument: could not parse '%s'" % colname)
    raise utils.BayesDBParseError("Invalid where clause argument: could not parse '%s'" % whereclause)
  return conds

def is_row_valid(idx, row, where_conditions, M_c, X_L_list, X_D_list, T, backend, tablename):
  """Helper function that applies WHERE conditions to row, returning True if row satisfies where clause."""
  for ((func, f_args), op, val) in where_conditions:
    where_value = func(f_args, idx, row, M_c, X_L_list, X_D_list, T, backend)    
    if func != functions._row_id:
      if not op(where_value, val):
        return False
    else:
      ## val should be a row list name in this case. look up the row list, and set val to be the list of row indices
      ## in the row list. Throws BayesDBRowListDoesNotExistError if row list does not exist.
      val = backend.persistence_layer.get_row_list(tablename, val)
      if not op(val, where_value): # for operator.contains, op(a,b) means 'b in a': so need to switch args.
        return False
  return True

def convert_row_from_codes_to_values(row, M_c):
  """
  Helper function to convert a row from its 'code' (as it's stored in T) to its 'value'
  (the human-understandable value).
  """
  ret = []
  for cidx, code in enumerate(row): 
    if not numpy.isnan(code) and not code=='nan':
      ret.append(du.convert_code_to_value(M_c, cidx, code))
    else:
      ret.append(code)
  return tuple(ret)

def filter_and_impute_rows(where_conditions, whereclause, T, M_c, X_L_list, X_D_list, engine, query_colnames,
                           impute_confidence, num_impute_samples, tablename):
    """
    impute_confidence: if None, don't impute. otherwise, this is the imput confidence
    Iterate through all rows of T, convert codes to values, filter by all predicates in where clause,
    and fill in imputed values.
    """
    filtered_rows = list()

    if impute_confidence is not None:
      t_array = numpy.array(T, dtype=float)
      infer_colnames = query_colnames[1:] # remove row_id from front of query_columns, so that infer doesn't infer row_id
      query_col_indices = [M_c['name_to_idx'][colname] for colname in infer_colnames]

    for row_id, T_row in enumerate(T):
      row_values = convert_row_from_codes_to_values(T_row, M_c) ## Convert row from codes to values
      if is_row_valid(row_id, row_values, where_conditions, M_c, X_L_list, X_D_list, T, engine, tablename): ## Where clause filtering.
        if impute_confidence is not None:
          ## Determine which values are 'nan', which need to be imputed.
          ## Only impute columns in 'query_colnames'
          for col_id in query_col_indices:
            if numpy.isnan(t_array[row_id, col_id]):
              # Found missing value! Try to fill it in.
              # row_id, col_id is Q. Y is givens: All non-nan values in this row
              Y = [(row_id, cidx, t_array[row_id, cidx]) for cidx in M_c['name_to_idx'].values() \
                   if not numpy.isnan(t_array[row_id, cidx])]
              code = utils.infer(M_c, X_L_list, X_D_list, Y, row_id, col_id, num_impute_samples,
                                 impute_confidence, engine)
              if code is not None:
                # Inferred successfully! Fill in the new value.
                value = du.convert_code_to_value(M_c, col_id, code)
                row_values = list(row_values)
                row_values[col_id] = value
                row_values = tuple(row_values)
        filtered_rows.append((row_id, row_values))
    return filtered_rows

def order_rows(rows, order_by, M_c, X_L_list, X_D_list, T, engine, column_lists):
  """Input: rows are list of (row_id, row_values) tuples."""
  if not order_by:
      return rows
  ## Step 1: get appropriate functions. Examples are 'column' and 'similarity'.
  function_list = list()
  for orderable in order_by:
    assert type(orderable) == tuple and type(orderable[0]) == str and type(orderable[1]) == bool
    raw_orderable_string = orderable[0]
    desc = orderable[1]

    ## function_list is a list of
    ##   (f(args, row_id, data_values, M_c, X_L_list, X_D_list, engine), args, desc)
    
    s = functions.parse_similarity(raw_orderable_string, M_c, T, column_lists)
    if s:
      function_list.append((functions._similarity, s, desc))
      continue

    c = functions.parse_row_typicality(raw_orderable_string)
    if c:
      function_list.append((functions._row_typicality, c, desc))
      continue

    p = functions.parse_predictive_probability(raw_orderable_string, M_c)
    if p is not None:
      function_list.append((functions._predictive_probability, p, desc))
      continue

    if raw_orderable_string.lower() in M_c['name_to_idx']:
      function_list.append((functions._column, M_c['name_to_idx'][raw_orderable_string.lower()], desc))
      continue

    raise utils.BayesDBParseError("Invalid query argument: could not parse '%s'" % raw_orderable_string)

  ## Step 2: call order by.
  rows = _order_by(rows, function_list, M_c, X_L_list, X_D_list, T, engine)
  return rows

def _order_by(filtered_values, function_list, M_c, X_L_list, X_D_list, T, engine):
  """
  Return the original data tuples, but sorted by the given functions.
  The data_tuples must contain all __original__ data because you can order by
  data that won't end up in the final result set.
  """
  if len(filtered_values) == 0 or not function_list:
    return filtered_values

  scored_data_tuples = list() ## Entries are (score, data_tuple)
  for row_id, data_tuple in filtered_values:
    ## Apply each function to each data_tuple to get a #functions-length tuple of scores.
    scores = []
    for (f, args, desc) in function_list:
      score = f(args, row_id, data_tuple, M_c, X_L_list, X_D_list, T, engine)
      if desc:
        score *= -1
      scores.append(score)
    scored_data_tuples.append((tuple(scores), (row_id, data_tuple)))
  scored_data_tuples.sort(key=lambda tup: tup[0], reverse=False)

  return [tup[1] for tup in scored_data_tuples]


def compute_result_and_limit(rows, limit, queries, M_c, X_L_list, X_D_list, T, engine):
  data = []
  row_count = 0

  # Compute aggregate functions just once, then cache them.
  aggregate_cache = dict()
  for query_idx, (query_function, query_args, aggregate) in enumerate(queries):
    if aggregate:
      aggregate_cache[query_idx] = query_function(query_args, None, None, M_c, X_L_list, X_D_list, T, engine)

  # Only return one row if all aggregate functions (row_id will never be aggregate, so subtract 1 and don't return it).
  assert queries[0][0] == functions._row_id
  if len(aggregate_cache) == len(queries) - 1:
    limit = 1

  # Iterate through data table, calling each query_function to fill in the output values.
  for row_id, row_values in rows:
    ret_row = []
    for query_idx, (query_function, query_args, aggregate) in enumerate(queries):
      if aggregate:
        ret_row.append(aggregate_cache[query_idx])
      else:
        ret_row.append(query_function(query_args, row_id, row_values, M_c, X_L_list, X_D_list, T, engine))
    data.append(tuple(ret_row))
    row_count += 1
    if row_count >= limit:
      break
  return data
