if ({
    &&& r1.wf()
    &&& r1.start == va
    &&& r1.len == len
    &&& r2.wf()
    &&& r2.start == va
    &&& r2.len == len
}) {
    reveal(VaRange4K::view_match_spec);
    assert(r1@.len() == r2@.len());
    assert forall|i: int| 0 <= i < r1@.len()
        implies r1@[i] == r2@[i] by {
        let j = i as usize;
        assert(j as int == i);
        assert(spec_va_add_range(r1.start, j) == r1@[j as int]);
        assert(spec_va_add_range(r2.start, j) == r2@[j as int]);
    }
    assert(r1@ =~= r2@);
}
