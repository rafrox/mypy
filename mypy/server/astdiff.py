"""Utilities for comparing two versions of a module symbol table.

The goal is to find which AST nodes have externally visible changes, so
that we can fire triggers and re-process other parts of the program
that are stale because of the changes.

Only look at detail at definitions at the current module -- don't
recurse into other modules.

A summary of the module contents:

* snapshot_symbol_table(...) creates an opaque snapshot description of a
  module/class symbol table (recursing into nested class symbol tables).

* compare_symbol_table_snapshots(...) compares two snapshots for the same
  module id and returns fully qualified names of differences (which act as
  triggers).

To compare two versions of a module symbol table, take snapshots of both
versions and compare the snapshots. The use of snapshots makes it easy to
compare two versions of the *same* symbol table that is being mutated.

Summary of how this works for certain kinds of differences:

* If a symbol table node is deleted or added (only present in old/new version
  of the symbol table), it is considered different, of course.

* If a symbol table node refers to a different sort of thing in the new version,
  it is considered different (for example, if a class is replaced with a
  function).

* If the signature of a function has changed, it is considered different.

* If the type of a variable changes, it is considered different.

* If the MRO of a class changes, or a non-generic class is turned into a
  generic class, the class is considered different (there are other such "big"
  differences that cause a class to be considered changed). However, just changes
  to attributes or methods don't generally constitute a difference at the
  class level -- these are handled at attribute level (say, 'mod.Cls.method'
  is different rather than 'mod.Cls' being different).

* If an imported name targets a different name (say, 'from x import y' is
  replaced with 'from z import y'), the name in the module is considered
  different. If the target of an import continues to have the same name,
  but it's specifics change, this doesn't mean that the imported name is
  treated as changed. Say, there is 'from x import y' in 'm', and the
  type of 'x.y' has changed. This doesn't mean that that 'm.y' is considered
  changed. Instead, processing the difference in 'm' will be handled through
  fine-grained dependencies.
"""

from typing import Set, List, TypeVar, Dict, Tuple, Optional, Sequence, Union

from mypy.nodes import (
    SymbolTable, SymbolTableNode, TypeInfo, Var, MypyFile, SymbolNode, Decorator, TypeVarExpr,
    OverloadedFuncDef, FuncItem, MODULE_REF, TYPE_ALIAS, UNBOUND_IMPORTED, TVAR
)
from mypy.types import (
    Type, TypeVisitor, UnboundType, TypeList, AnyType, NoneTyp, UninhabitedType,
    ErasedType, DeletedType, Instance, TypeVarType, CallableType, TupleType, TypedDictType,
    UnionType, Overloaded, PartialType, TypeType, function_type
)
from mypy.util import get_prefix


# Snapshot representation of a symbol table node or type. The representation is
# opaque -- the only supported operations are comparing for equality and
# hashing (latter for type snapshots only). Snapshots can contain primitive
# objects, nested tuples, lists and dictionaries and primitive objects (type
# snapshots are immutable).
#
# For example, the snapshot of the 'int' type is ('Instance', 'builtins.int', ()).
SnapshotItem = Tuple[object, ...]


def compare_symbol_table_snapshots(
        name_prefix: str,
        snapshot1: Dict[str, SnapshotItem],
        snapshot2: Dict[str, SnapshotItem]) -> Set[str]:
    """Return names that are different in two snapshots of a symbol table.

    Only shallow (intra-module) differences are considered. References to things defined
    outside the module are compared based on the name of the target only.

    Recurse into class symbol tables (if the class is defined in the target module).

    Return a set of fully-qualified names (e.g., 'mod.func' or 'mod.Class.method').
    """
    # Find names only defined only in one version.
    names1 = {'%s.%s' % (name_prefix, name) for name in snapshot1}
    names2 = {'%s.%s' % (name_prefix, name) for name in snapshot2}
    triggers = names1 ^ names2

    # Look for names defined in both versions that are different.
    for name in set(snapshot1.keys()) & set(snapshot2.keys()):
        item1 = snapshot1[name]
        item2 = snapshot2[name]
        kind1 = item1[0]
        kind2 = item2[0]
        item_name = '%s.%s' % (name_prefix, name)
        if kind1 != kind2:
            # Different kind of node in two snapshots -> trivially different.
            triggers.add(item_name)
        elif kind1 == 'TypeInfo':
            if item1[:-1] != item2[:-1]:
                # Record major difference (outside class symbol tables).
                triggers.add(item_name)
            # Look for differences in nested class symbol table entries.
            assert isinstance(item1[-1], dict)
            assert isinstance(item2[-1], dict)
            triggers |= compare_symbol_table_snapshots(item_name, item1[-1], item2[-1])
        else:
            # Shallow node (no interesting internal structure). Just use equality.
            if snapshot1[name] != snapshot2[name]:
                triggers.add(item_name)

    return triggers


def snapshot_symbol_table(name_prefix: str, table: SymbolTable) -> Dict[str, SnapshotItem]:
    """Create a snapshot description that represents the state of a symbol table.

    The snapshot has a representation based on nested tuples and dicts
    that makes it easy and fast to find differences.

    Only "shallow" state is included in the snapshot -- references to
    things defined in other modules are represented just by the names of
    the targets.
    """
    result = {}  # type: Dict[str, SnapshotItem]
    for name, symbol in table.items():
        node = symbol.node
        # TODO: cross_ref?
        fullname = node.fullname() if node else None
        common = (fullname, symbol.kind, symbol.module_public)
        if symbol.kind == MODULE_REF:
            # This is a cross-reference to another module.
            assert isinstance(node, MypyFile)
            result[name] = ('Moduleref', common)
        elif symbol.kind == TVAR:
            assert isinstance(node, TypeVarExpr)
            result[name] = ('TypeVar',
                            node.variance,
                            [snapshot_type(value) for value in node.values],
                            snapshot_type(node.upper_bound))
        elif symbol.kind == TYPE_ALIAS:
            result[name] = ('TypeAlias',
                            symbol.alias_tvars,
                            snapshot_optional_type(symbol.type_override))
        else:
            assert symbol.kind != UNBOUND_IMPORTED
            if node and get_prefix(node.fullname()) != name_prefix:
                # This is a cross-reference to a node defined in another module.
                result[name] = ('CrossRef', common, symbol.normalized)
            else:
                result[name] = snapshot_definition(node, common)
    return result


def snapshot_definition(node: Optional[SymbolNode],
                        common: Tuple[object, ...]) -> Tuple[object, ...]:
    """Create a snapshot description of a symbol table node.

    The representation is nested tuples and dicts. Only externally
    visible attributes are included.
    """
    if isinstance(node, (OverloadedFuncDef, FuncItem)):
        # TODO: info
        if node.type:
            signature = snapshot_type(node.type)
        else:
            signature = snapshot_untyped_signature(node)
        return ('Func', common, node.is_property, signature)
    elif isinstance(node, Var):
        return ('Var', common, snapshot_optional_type(node.type))
    elif isinstance(node, Decorator):
        # Note that decorated methods are represented by Decorator instances in
        # a symbol table since we need to preserve information about the
        # decorated function (whether it's a class function, for
        # example). Top-level decorated functions, however, are represented by
        # the corresponding Var node, since that happens to provide enough
        # context.
        return ('Decorator',
                node.is_overload,
                snapshot_optional_type(node.var.type),
                snapshot_definition(node.func, common))
    elif isinstance(node, TypeInfo):
        attrs = (node.is_abstract,
                 node.is_enum,
                 node.fallback_to_any,
                 node.is_named_tuple,
                 node.is_newtype,
                 snapshot_optional_type(node.tuple_type),
                 snapshot_optional_type(node.typeddict_type),
                 [base.fullname() for base in node.mro],
                 node.type_vars,
                 [snapshot_type(base) for base in node.bases],
                 snapshot_optional_type(node._promote))
        prefix = node.fullname()
        symbol_table = snapshot_symbol_table(prefix, node.names)
        return ('TypeInfo', common, attrs, symbol_table)
    else:
        # Other node types are handled elsewhere.
        assert False, type(node)


def snapshot_type(typ: Type) -> SnapshotItem:
    """Create a snapshot representation of a type using nested tuples."""
    return typ.accept(SnapshotTypeVisitor())


def snapshot_optional_type(typ: Optional[Type]) -> Optional[SnapshotItem]:
    if typ:
        return snapshot_type(typ)
    else:
        return None


def snapshot_types(types: Sequence[Type]) -> SnapshotItem:
    return tuple(snapshot_type(item) for item in types)


def snapshot_simple_type(typ: Type) -> SnapshotItem:
    return (type(typ).__name__,)


class SnapshotTypeVisitor(TypeVisitor[SnapshotItem]):
    """Creates a read-only, self-contained snapshot of a type object.

    Properties of a snapshot:

    - Contains (nested) tuples and other immutable primitive objects only.
    - References to AST nodes are replaced with full names of targets.
    - Has no references to mutable or non-primitive objects.
    - Two snapshots represent the same object if and only if they are
      equal.
    """

    def visit_unbound_type(self, typ: UnboundType) -> SnapshotItem:
        return ('UnboundType',
                typ.name,
                typ.optional,
                typ.empty_tuple_index,
                snapshot_types(typ.args))

    def visit_any(self, typ: AnyType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_none_type(self, typ: NoneTyp) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_uninhabited_type(self, typ: UninhabitedType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_erased_type(self, typ: ErasedType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_deleted_type(self, typ: DeletedType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_instance(self, typ: Instance) -> SnapshotItem:
        return ('Instance',
                typ.type.fullname(),
                snapshot_types(typ.args))

    def visit_type_var(self, typ: TypeVarType) -> SnapshotItem:
        return ('TypeVar',
                typ.name,
                typ.fullname,
                typ.id.raw_id,
                typ.id.meta_level,
                snapshot_types(typ.values),
                snapshot_type(typ.upper_bound),
                typ.variance)

    def visit_callable_type(self, typ: CallableType) -> SnapshotItem:
        # FIX generics
        return ('CallableType',
                snapshot_types(typ.arg_types),
                snapshot_type(typ.ret_type),
                tuple(typ.arg_names),
                tuple(typ.arg_kinds),
                typ.is_type_obj(),
                typ.is_ellipsis_args)

    def visit_tuple_type(self, typ: TupleType) -> SnapshotItem:
        return ('TupleType', snapshot_types(typ.items))

    def visit_typeddict_type(self, typ: TypedDictType) -> SnapshotItem:
        items = tuple((key, snapshot_type(item_type))
                      for key, item_type in typ.items.items())
        required = tuple(sorted(typ.required_keys))
        return ('TypedDictType', items, required)

    def visit_union_type(self, typ: UnionType) -> SnapshotItem:
        # Sort and remove duplicates so that we can use equality to test for
        # equivalent union type snapshots.
        items = {snapshot_type(item) for item in typ.items}
        normalized = tuple(sorted(items))
        return ('UnionType', normalized)

    def visit_overloaded(self, typ: Overloaded) -> SnapshotItem:
        return ('Overloaded', snapshot_types(typ.items()))

    def visit_partial_type(self, typ: PartialType) -> SnapshotItem:
        # A partial type is not fully defined, so the result is indeterminate. We shouldn't
        # get here.
        raise RuntimeError

    def visit_type_type(self, typ: TypeType) -> SnapshotItem:
        return ('TypeType', snapshot_type(typ.item))


def snapshot_untyped_signature(func: Union[OverloadedFuncDef, FuncItem]) -> Tuple[object, ...]:
    """Create a snapshot of the signature of a function that has no explicit signature.

    If the arguments to a function without signature change, it must be
    considered as different. We have this special casing since we don't store
    the implicit signature anywhere, and we'd rather not construct new
    Callable objects in this module (the idea is to only read properties of
    the AST here).
    """
    if isinstance(func, FuncItem):
        return (tuple(func.arg_names), tuple(func.arg_kinds))
    else:
        result = []
        for item in func.items:
            if isinstance(item, Decorator):
                if item.var.type:
                    result.append(snapshot_type(item.var.type))
                else:
                    result.append(('DecoratorWithoutType',))
            else:
                result.append(snapshot_untyped_signature(item))
        return tuple(result)
