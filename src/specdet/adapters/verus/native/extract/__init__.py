"""Verus-native specification types and lossless serialization."""

from .types import FunctionSpec, Param, TypeInfo


def function_spec_to_dict(spec: FunctionSpec) -> dict:
    return {
        "name": spec.name,
        "params": [
            {
                "name": param.name,
                "type": param.type.to_dict(),
                "is_mut_ref": param.is_mut_ref,
                "is_ref": param.is_ref,
                "is_self": param.is_self,
                "destructure_ctor": param.destructure_ctor,
            }
            for param in spec.params
        ],
        "return_type": spec.return_type.to_dict(),
        "requires": list(spec.requires),
        "ensures": list(spec.ensures),
        "type_defs": {name: ty.to_dict() for name, ty in spec.type_defs.items()},
        "result_binding": spec.result_binding,
        "generics_decl": spec.generics_decl,
        "where_decl": spec.where_decl,
        "self_type": spec.self_type,
        "trait_name": spec.trait_name,
    }


def function_spec_from_dict(data: dict) -> FunctionSpec:
    return FunctionSpec(
        name=data["name"],
        params=[
            Param(
                name=param["name"],
                type=TypeInfo.from_dict(param["type"]),
                is_mut_ref=param.get("is_mut_ref", False),
                is_ref=param.get("is_ref", False),
                is_self=param.get("is_self", False),
                destructure_ctor=param.get("destructure_ctor"),
            )
            for param in data["params"]
        ],
        return_type=TypeInfo.from_dict(data["return_type"]),
        requires=list(data["requires"]),
        ensures=list(data["ensures"]),
        type_defs={
            name: TypeInfo.from_dict(ty)
            for name, ty in data.get("type_defs", {}).items()
        },
        result_binding=data.get("result_binding", "result"),
        generics_decl=data.get("generics_decl", ""),
        where_decl=data.get("where_decl", ""),
        self_type=data.get("self_type"),
        trait_name=data.get("trait_name"),
    )


__all__ = ["function_spec_to_dict", "function_spec_from_dict"]
