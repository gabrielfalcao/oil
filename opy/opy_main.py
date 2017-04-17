#!/usr/bin/python
"""
opy_main.py
"""
from __future__ import print_function

import cStringIO
import codecs
import io
import os
import sys
import marshal
import logging

from pgen2 import driver, pgen, grammar
from pgen2 import token, tokenize
import pytree

from compiler2 import transformer
from compiler2 import pycodegen
from compiler2 import opcode

from byterun import execfile

from util import log
import util


# From lib2to3/pygram.py.  This presumably takes the place of the 'symbol'
# module.  Could we hook it up elsewhere?
#
# compiler/transformer module needs this.
# tools/compile.py runs compileFile, which runs parseFile.

class Symbols(object):

    def __init__(self, gr):
        """Initializer.

        Creates an attribute for each grammar symbol (nonterminal),
        whose value is the symbol's type (an int >= 256).
        """
        for name, symbol in gr.symbol2number.items():
            setattr(self, name, symbol)
            #log('%s -> %d' % (name, symbol))
        # For transformer to use
        self.number2symbol = gr.number2symbol
        #assert 0


def HostStdlibNames():
  import symbol
  import token
  names = {}
  for k, v in symbol.sym_name.items():
    names[k] = v
  for k, v in token.tok_name.items():
    names[k] = v
  return names


def _newer(a, b):
    """Inquire whether file a was written since file b."""
    if not os.path.exists(a):
        return False
    if not os.path.exists(b):
        return True
    return os.path.getmtime(a) >= os.path.getmtime(b)


def _generate_pickle_name(gt):
    head, tail = os.path.splitext(gt)
    if tail == ".txt":
        tail = ""
    return head + tail + ".".join(map(str, sys.version_info)) + ".pickle"


def load_grammar(gt="Grammar.txt", gp=None,
                 save=True, force=False, logger=None):
    """Load the grammar (maybe from a pickle)."""
    if logger is None:
        logger = logging.getLogger()
    gp = _generate_pickle_name(gt) if gp is None else gp
    if force or not _newer(gp, gt):
        logger.info("Generating grammar tables from %s", gt)
        g = pgen.generate_grammar(gt)
        if save:
            logger.info("Writing grammar tables to %s", gp)
            try:
                g.dump(gp)
            except OSError as e:
                logger.info("Writing failed: %s", e)
    else:
        g = grammar.Grammar()
        g.load(gp)
    return g


_READ_SOURCE_AS_UNICODE = True
# Not sure why this doesn't work?  It should be more like what the compiler
# module expected.
#_READ_SOURCE_AS_UNICODE = False

# Emulate the interface that Transformer expects from parsermodule.c.
class Pgen2PythonParser:
  def __init__(self, driver, start_symbol):
    self.driver = driver
    self.start_symbol = start_symbol

  def suite(self, text):
    # Python 3
    #f = io.StringIO(text)
    f = cStringIO.StringIO(text)
    tokens = tokenize.generate_tokens(f.readline)
    tree = self.driver.parse_tokens(tokens, start_symbol=self.start_symbol)
    return tree


def CountTupleTree(tu):
  """Count the nodes in a tuple parse tree."""
  if isinstance(tu, tuple):
    s = 0
    for entry in tu:
      s += CountTupleTree(entry)
    return s
  elif isinstance(tu, int):
    return 1
  elif isinstance(tu, str):
    return 1
  else:
    raise AssertionError(tu)


class TupleTreePrinter:
  def __init__(self, names):
    self._names = names

  def Print(self, tu, f=sys.stdout, indent=0):
    ind = '  ' * indent
    f.write(ind)
    if isinstance(tu, tuple):
      f.write(self._names[tu[0]])
      f.write('\n')
      for entry in tu[1:]:
        self.Print(entry, f, indent=indent+1)
    elif isinstance(tu, int):
      f.write(str(tu))
      f.write('\n')
    elif isinstance(tu, str):
      f.write(str(tu))
      f.write('\n')
    else:
      raise AssertionError(tu)


def main(argv):
  grammar_path = argv[1]
  # NOTE: This is cached as a pickle
  gr = load_grammar(grammar_path)
  FILE_INPUT = gr.symbol2number['file_input']

  symbols = Symbols(gr)
  pytree.Init(symbols)  # for type_repr() pretty printing
  transformer.Init(symbols)  # for _names and other dicts

  # In Python 2 code, always use from __future__ import print_function.
  if 1:  # TODO: re-enable after 2to3
    try:
      del gr.keywords["print"]
    except KeyError:
      pass

  #do_glue = False
  do_glue = True

  if do_glue:  # Make it a flag
    # Emulating parser.st structures from parsermodule.c.
    # They have a totuple() method, which outputs tuples like this.
    def py2st(gr, raw_node):
      type, value, context, children = raw_node
      # See pytree.Leaf
      if context:
        _, (lineno, column) = context
      else:
        lineno = 0  # default in Leaf
        column = 0

      if children:
        return (type,) + tuple(children)
      else:
        return (type, value, lineno, column)
    convert = py2st
  else:
    convert = pytree.convert

  dr = driver.Driver(gr, convert=convert)

  action = argv[2]

  if action == 'stdlib-parse':
    # This is what the compiler/ package was written against.
    import parser

    py_path = argv[3]
    with open(py_path) as f:
      st = parser.suite(f.read())

    tree = st.totuple()

    n = transformer.CountTupleTree(tree)
    log('COUNT %d', n)
    printer = TupleTreePrinter(HostStdlibNames())
    printer.Print(tree)

  elif action == 'parse':
    py_path = argv[3]
    with open(py_path) as f:
      tokens = tokenize.generate_tokens(f.readline)
      tree = dr.parse_tokens(tokens, start_symbol=FILE_INPUT)

    if isinstance(tree, tuple):
      n = CountTupleTree(tree)
      log('COUNT %d', n)

      printer = TupleTreePrinter(transformer._names)
      printer.Print(tree)
    else:
      tree.PrettyPrint(sys.stdout)
      log('\tChildren: %d' % len(tree.children), file=sys.stderr)

  elif action == 'old-compile':
    py_path = argv[3]
    out_path = argv[4]

    if do_glue:
      py_parser = Pgen2PythonParser(dr, FILE_INPUT)
      printer = TupleTreePrinter(transformer._names)
      tr = transformer.Pgen2Transformer(py_parser, printer)
    else:
      tr = transformer.Transformer()

    # for Python 2.7 compatibility:
    if _READ_SOURCE_AS_UNICODE:
      f = codecs.open(py_path, encoding='utf-8')
    else:
      f = open(py_path)

    contents = f.read()
    co = pycodegen.compile(contents, py_path, 'exec', transformer=tr)
    log("Code length: %d", len(co.co_code))

    # Write the .pyc file
    with open(out_path, 'wb') as out_f:
      h = pycodegen.getPycHeader(py_path)
      out_f.write(h)
      marshal.dump(co, out_f)

  elif action == 'compile':
    # 'opy compile' is pgen2 + compiler2
    # TODO: import compiler2
    #raise NotImplementedError
    py_path = argv[3]
    out_path = argv[4]

    if do_glue:
      py_parser = Pgen2PythonParser(dr, FILE_INPUT)
      printer = TupleTreePrinter(transformer._names)
      tr = transformer.Pgen2Transformer(py_parser, printer)
    else:
      tr = transformer.Transformer()

    with open(py_path) as f:
      contents = f.read()
    co = pycodegen.compile(contents, py_path, 'exec', transformer=tr)
    log("Code length: %d", len(co.co_code))

    # Write the .pyc file
    with open(out_path, 'wb') as out_f:
      h = pycodegen.getPycHeader(py_path)
      out_f.write(h)
      marshal.dump(co, out_f)

  elif action == 'compile2':
    in_path = argv[3]
    out_path = argv[4]

    from compiler2 import pycodegen as pycodegen2
    from misc import stdlib_compile

    stdlib_compile.compileAndWrite(in_path, out_path, pycodegen2.compile)

  elif action == 'run':
    # TODO: Add an option like -v in __main__

    #level = logging.DEBUG if args.verbose else logging.WARNING
    #logging.basicConfig(level=level)

    # Compile and run, without writing pyc file
    py_path = argv[3]
    opy_argv = argv[3:]

    py_parser = Pgen2PythonParser(dr, FILE_INPUT)
    printer = TupleTreePrinter(transformer._names)
    tr = transformer.Pgen2Transformer(py_parser, printer)
    with open(py_path) as f:
      contents = f.read()
    co = pycodegen.compile(contents, py_path, 'exec', transformer=tr)
    execfile.run_code_object(co, opy_argv)

  else: 
    raise RuntimeError('Invalid action %r' % action)

  # Examples of nodes Leaf(type, value):
  #   Leaf(1, 'def')
  #   Leaf(4, '\n')
  #   Leaf(8, ')')
  # Oh are these just tokens?
  # yes.

  # Node(prefix, children)


if __name__ == '__main__':
  try:
    main(sys.argv)
  except RuntimeError as e:
    log('FATAL: %s', e)
    sys.exit(1)
