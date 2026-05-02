# `Feature`

::: gffbase.feature.Feature
    options:
      show_root_heading: true
      members_order: source
      show_signature_annotations: true
      separate_signature: true
      filters:
        - "!^_"

## `ParsedFeature` (parser-internal record)

The slotted dataclass the Rust+Python parser emits before features land
in the database.

::: gffbase.feature.ParsedFeature
    options:
      show_root_heading: true
      members_order: source
      show_signature_annotations: true
      filters:
        - "!^_"
