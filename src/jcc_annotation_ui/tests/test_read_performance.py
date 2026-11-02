import hashlib
import json
import os
from unittest.mock import patch

from src.jcc_samples import SampleStore, WorkflowError
from src.jcc_samples import workflow as wf
from src.jcc_annotation_ui.workspace import AnnotationWorkspace
from src.jcc_annotation_ui.tests.helpers import PrivateFixture
from src.jcc_annotation_ui.tests.scale_fixture import make_scale_fixture


class VerifiedReadTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.root=self.base/'scale-small'
        fixture=make_scale_fixture(self.root,count=4,size=(100,60))
        self.sample_id=fixture['sample_ids'][0]
        self.other_id=fixture['sample_ids'][1]
        self.store=SampleStore(self.root)
        self.ui=AnnotationWorkspace(self.root,sample_kind='synthetic')

    def asset(self, sample_id=None):
        return self.root/self.data()['assets'][sample_id or self.sample_id]['path']

    def test_cold_inventory_defers_decode_and_selected_image_decodes_once(self):
        decoder=wf._image_info
        with patch.object(wf,'_image_info',wraps=decoder) as calls:
            ui=AnnotationWorkspace(self.root,sample_kind='synthetic')
            listing=ui.samples()
            self.assertEqual(calls.call_count,0)
            self.assertEqual(listing['verification']['decoded_assets'],0)
            self.assertEqual(listing['verification']['byte_checked_assets'],4)
            view=ui.sample(self.sample_id)
            self.assertEqual(calls.call_count,1)
            self.assertEqual(view['verification']['decoded_assets'],1)
            ui.image(self.anchor(view))
            ui.samples();ui.sample(self.sample_id)
            self.assertEqual(calls.call_count,1)

    def test_full_preflight_is_read_only_and_skips_no_unseen_decode(self):
        before=self.snapshot()
        decoder=wf._image_info
        with patch.object(wf,'_image_info',wraps=decoder) as calls:
            self.ui.sample(self.sample_id)
            result=self.ui.verify()
            self.assertEqual(calls.call_count,4)
            self.assertEqual(result['verification']['fresh_decodes'],3)
            self.assertEqual(result['verification']['decoded_assets'],4)
            self.assertEqual(self.ui.verify()['verification']['fresh_decodes'],0)
            self.assertEqual(calls.call_count,4)
        self.assertEqual(self.snapshot(),before)

    def test_submit_full_verification_even_if_only_one_image_was_viewed(self):
        decoder=wf._image_info
        with patch.object(wf,'_image_info',wraps=decoder) as calls:
            view=self.view()
            self.assertEqual(calls.call_count,1)
            self.ui.annotate(self.edit(view))
            self.assertEqual(calls.call_count,4)
            self.ui.review(self.review())
            self.assertEqual(calls.call_count,4)
        self.assertEqual(self.data()['revision'],6)

    def test_warm_proof_never_bypasses_fresh_bytes_size_or_mtime(self):
        self.ui.verify()
        asset=self.asset(self.other_id);original=asset.read_bytes();info=asset.stat()
        changed=bytearray(original);changed[-1]^=1
        asset.write_bytes(changed)
        os.utime(asset,ns=(info.st_atime_ns,info.st_mtime_ns))
        self.assertEqual(asset.stat().st_size,info.st_size)
        for operation in (self.ui.samples,lambda:self.ui.sample(self.sample_id),self.ui.verify):
            self.rejects('asset_integrity',operation)

    def test_warm_proof_still_rechecks_permissions_links_and_binding(self):
        self.ui.verify()
        asset=self.asset(self.other_id)
        asset.chmod(0o644)
        self.rejects('unsafe_permissions',self.ui.samples)
        asset.chmod(0o600)
        link=self.base/'hard-link';os.link(asset,link)
        self.rejects('unsafe_permissions',self.ui.verify)
        link.unlink()
        data=self.data();data['assets'][self.other_id]['pixel_sha256']='0'*64
        (self.root/'workspace.json').write_text(json.dumps(data))
        self.rejects('asset_integrity',self.ui.samples)

    def test_new_matching_hash_requires_decode_before_any_submit(self):
        # A forged index and corrupt bytes may pass the explicitly byte-only
        # inventory. It must NOT claim decoded verification or pass any write.
        asset=self.asset(self.other_id);raw=b'not-a-png-with-forged-hash'
        asset.write_bytes(raw);data=self.data()
        data['assets'][self.other_id]['sha256']=hashlib.sha256(raw).hexdigest()
        (self.root/'workspace.json').write_text(json.dumps(data))
        listing=self.ui.samples()
        self.assertEqual(listing['verification']['decoded_assets'],0)
        view=self.view()
        self.rejects('image_unreadable',lambda:self.ui.annotate(self.edit(view)))
        self.rejects('image_unreadable',lambda:self.ui.review(self.review(view)))
        self.rejects('image_unreadable',lambda:self.ui.sample(self.other_id))

    def test_replaced_valid_bytes_and_index_decode_again(self):
        self.ui.verify()
        other=self.asset(self.other_id);source=self.base/'replacement.png'
        from PIL import Image
        Image.new('RGB',(100,60),(111,22,44)).save(source)
        raw=source.read_bytes();info=wf._image_info(raw);other.write_bytes(raw)
        data=self.data();data['assets'][self.other_id].update(info)
        (self.root/'workspace.json').write_text(json.dumps(data))
        listing=self.ui.samples()
        self.assertEqual(listing['verification']['decoded_assets'],3)
        checked=self.ui.sample(self.other_id)
        self.assertEqual(checked['verification']['fresh_decodes'],1)
        self.assertEqual(checked['verification']['decoded_assets'],4)

    def test_decoder_limit_change_and_cache_eviction_require_new_proof(self):
        self.ui.verify()
        with patch.object(wf,'MAX_PIXELS',100):
            self.assertEqual(self.ui.samples()['verification']['decoded_assets'],0)
            self.rejects('image_too_large',self.ui.verify)
        self.ui.store.MAX_PROOFS=2
        self.ui.store.clear_proofs()
        self.ui.verify()
        self.assertLessEqual(len(self.ui.store._proofs),2)
        self.assertEqual(self.ui.samples()['verification']['decoded_assets'],2)

    def test_corrupt_unseen_asset_blocks_full_preflight_and_legacy_store_too(self):
        # Same checks as the original full verifier for index pixel/geometry
        # damage, including a cold proof cache. Inventory doesn't certify these.
        original=(self.root/'workspace.json').read_bytes()
        mutations=(lambda d:d['assets'][self.other_id].update(pixel_sha256='0'*64),
                   lambda d:d['assets'][self.other_id].update(width_px=99),
                   lambda d:d['annotations'][1]['frame']['image'].update(width_px=99))
        for change in mutations:
            data=json.loads(original);change(data)
            (self.root/'workspace.json').write_text(json.dumps(data))
            for operation in (self.store.validate,self.ui.verify):
                with self.assertRaises(WorkflowError): operation()
            (self.root/'workspace.json').write_bytes(original)
            self.ui.close()

    def test_signature_rejects_label_change_without_revision_even_after_warming(self):
        self.ui.verify();view=self.view()
        data=self.data();data['annotations'][0]['conditions']=['synthetic-modified']
        (self.root/'workspace.json').write_text(json.dumps(data))
        self.rejects('view_expired',lambda:self.ui.annotate(self.edit(view)))

    def test_close_drops_memory_proofs_without_persistence(self):
        self.ui.verify();before=self.snapshot();self.ui.close()
        self.assertEqual(len(self.ui.store._proofs),0)
        self.assertEqual(self.ui.samples()['verification']['decoded_assets'],0)
        self.assertEqual(self.snapshot(),before)
