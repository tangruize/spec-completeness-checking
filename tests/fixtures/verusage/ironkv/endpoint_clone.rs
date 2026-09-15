use vstd::prelude::*;

fn main() {}

verus! {

pub struct AbstractEndPoint {
    pub id: Seq<u8>,
}

#[derive(PartialEq, Eq, Hash)]
pub struct EndPoint {
    pub id: Vec<u8>,
}

impl EndPoint {

	#[verifier::external_body]
    pub fn clone_up_to_view(&self) -> (res: EndPoint)
        ensures res@ == self@
	{
		unimplemented!()
	}

    pub open spec fn view(self) -> AbstractEndPoint {
        AbstractEndPoint{id: self.id@}
    }

}

}
