# Multipart features

Several GFF3 lines may share one `ID` — the standard way to represent a CDS
interrupted by a frameshift, and how NCBI RefSeq encodes split CDS segments.
Under `mode="strict"` those lines become **one logical feature** with a
`segments` side table holding the physical lines.

`MultipartFeature` and `FeatureSegment` both subclass `Feature` and override
none of `__str__`, `__len__`, `__hash__`, `__eq__`, `__getitem__` or
`astuple` — the compatibility surface is preserved by inaction. `len()` stays
the envelope span; `covered_length` is the new quantity that excludes the
gaps.

::: gffbase.feature.MultipartFeature
    options:
      show_root_heading: true
      members_order: source
      filters:
        - "!^_"

::: gffbase.feature.FeatureSegment
    options:
      show_root_heading: true
      members_order: source
      filters:
        - "!^_"
