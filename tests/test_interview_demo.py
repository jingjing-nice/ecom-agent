"""面试演示的判定与报告不允许把错误数值或规则降级算作真实模型成功。"""
from scripts.interview_demo import check_cards, compare_rows


def test_sql_comparison_ignores_aliases_and_order_but_checks_values():
    reference = [{'category': '吸顶灯', 'paid_amount_minor': '95700.00'}]
    assert compare_rows([{'total': 95700, 'kind': '吸顶灯'}], reference)
    assert not compare_rows([{'total': 95701, 'kind': '吸顶灯'}], reference)
    assert not compare_rows([], [])
    assert not compare_rows([{'total': 95700, 'kind': '壁灯'}], reference)


def test_handoff_card_checks_constraints_and_excludes_original():
    card = {'product_id': 'alternative', 'category': '吸顶灯', 'brand': '面试栖家灯饰',
            'price_minor': 29900, 'available_qty': 20, 'specs': {'size': '42'}}
    assert check_cards({'cards': [card]}, size='42', exclude='original')
    for patch in ({'price_minor': 50001}, {'available_qty': 0}, {'product_id': 'original'},
                  {'specs': {'size': '41'}}, {'brand': '其他品牌'}):
        assert not check_cards({'cards': [{**card, **patch}]}, size='42', exclude='original')


def test_model_usage_export_only_contains_metering_fields(tmp_path):
    import json
    from scripts.interview_demo import collect_model_usage
    (tmp_path/'api.log').write_text(
        'private provider body must not be exported\n'
        '[model] chat purpose=agent.supervisor prompt_version=v1 model=qwen3.8-max in=100 out=20 latency=300ms cost~unknown\n')
    (tmp_path/'model_usage.log').write_text(
        '[model] structured purpose=product_image prompt_version= model=qwen3-vl-plus in=50 out=10 latency=100ms cost~unknown\n')
    result = collect_model_usage(tmp_path)
    assert result['input_tokens'] == 150 and result['output_tokens'] == 30
    assert len(result['observed_successful_chat_vision_calls']) == 2
    assert 'private provider body' not in json.dumps(result)
