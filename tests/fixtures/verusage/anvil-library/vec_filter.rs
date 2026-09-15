use vstd::prelude::*;
use vstd::seq_lib::*;

fn main() {}

verus! {

trait VerusClone: View + Sized {
    fn verus_clone(&self) -> (r: Self)
        ensures self == r;
}

#[verifier::external_body]
fn vec_filter<V: VerusClone + View + Sized>(v: Vec<V>, f: impl Fn(&V)->bool, f_spec: spec_fn(V)->bool) -> (r: Vec<V>)
    requires
        forall|v: V| #[trigger] f.requires((&v,)),
        forall |v:V,r:bool| f.ensures((&v,), r) ==> f_spec(v) == r,
    ensures r@.to_multiset() =~= v@.to_multiset().filter(f_spec)
{
    unimplemented!()
}

}
