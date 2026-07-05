/*
 * 雨温図の比較描画（D3）。
 * 単体表示はビルド時生成の静的 SVG を使い、?compare= のときだけこのスクリプトが
 * /data/climate/{slug}.json を取得して 2 地点の比較チャートを D3 で描く。
 * 色は旧サイト踏襲: 最高 #F92500 / 平均 #008000 / 最低 #0C00CC / 降水 #1987E5。
 */
(function () {
    'use strict';

    var MONTHS = ['1月', '2月', '3月', '4月', '5月', '6月',
                  '7月', '8月', '9月', '10月', '11月', '12月'];
    var LINES = [['tmax', '最高気温', '#F92500'],
                 ['tavg', '平均気温', '#008000'],
                 ['tmin', '最低気温', '#0C00CC']];

    function drawCompare(el, a, b) {
        el.innerHTML = '';
        var W = 760, H = 470, ml = 52, mr = 56, mt = 46, mb = 30;
        var pw = W - ml - mr, ph = H - mt - mb;

        var svg = d3.select(el).append('svg')
            .attr('viewBox', '0 0 ' + W + ' ' + H)
            .style('max-width', W + 'px').style('width', '100%')
            .style('height', 'auto').style('background', '#fff');

        var x = d3.scaleBand().domain(d3.range(12)).range([ml, ml + pw]).padding(0.18);
        var yT = d3.scaleLinear().domain([-20, 40]).range([mt + ph, mt]);
        var yP = d3.scaleLinear().domain([0, 600]).range([mt + ph, mt]);

        svg.append('text').attr('x', W / 2).attr('y', 20)
            .attr('text-anchor', 'middle').attr('font-size', 16).attr('font-weight', 'bold')
            .text(a.name + 'と' + b.name + 'の雨温図の比較');

        // グリッド・軸
        d3.range(-20, 41, 10).forEach(function (t) {
            svg.append('line').attr('x1', ml).attr('x2', ml + pw)
                .attr('y1', yT(t)).attr('y2', yT(t))
                .attr('stroke', t === 0 ? '#999' : '#e3e8ee');
            svg.append('text').attr('x', ml - 6).attr('y', yT(t) + 4)
                .attr('text-anchor', 'end').attr('font-size', 11).attr('fill', '#F92500')
                .text(t + '°C');
        });
        d3.range(0, 601, 100).forEach(function (p) {
            svg.append('text').attr('x', ml + pw + 6).attr('y', yP(p) + 4)
                .attr('font-size', 11).attr('fill', '#1987E5').text(p);
        });
        MONTHS.forEach(function (m, i) {
            svg.append('text').attr('x', x(i) + x.bandwidth() / 2).attr('y', mt + ph + 16)
                .attr('text-anchor', 'middle').attr('font-size', 11).attr('fill', '#555')
                .text(m);
        });

        // 降水量: 2 地点並列の棒
        var bw = x.bandwidth() / 2;
        [[a, '#1987E5', 0], [b, '#9DC8EF', 1]].forEach(function (cfg) {
            var st = cfg[0], color = cfg[1], k = cfg[2];
            svg.selectAll(null).data(st.monthly.precip).enter()
                .filter(function (d) { return d != null; })
                .append('rect')
                .attr('x', function (d, i) { return x(i) + k * bw; })
                .attr('y', function (d) { return yP(Math.min(d, 600)); })
                .attr('width', bw - 1)
                .attr('height', function (d) { return mt + ph - yP(Math.min(d, 600)); })
                .attr('fill', color).attr('fill-opacity', 0.9);
        });

        // 気温: 実線=基準地点、破線=比較地点
        var lineGen = d3.line()
            .defined(function (d) { return d != null; })
            .x(function (d, i) { return x(i) + x.bandwidth() / 2; })
            .y(function (d) { return yT(d); })
            .curve(d3.curveCatmullRom);
        [[a, 'none'], [b, '6,4']].forEach(function (cfg) {
            var st = cfg[0], dash = cfg[1];
            LINES.forEach(function (L) {
                svg.append('path').datum(st.monthly[L[0]])
                    .attr('d', lineGen)
                    .attr('fill', 'none').attr('stroke', L[2]).attr('stroke-width', 2)
                    .attr('stroke-dasharray', dash === 'none' ? null : dash);
            });
        });

        svg.append('rect').attr('x', ml).attr('y', mt).attr('width', pw).attr('height', ph)
            .attr('fill', 'none').attr('stroke', '#c8d2dc');

        // 凡例
        var lx = ml + 6;
        LINES.concat([['precip', '降水量', '#1987E5']]).forEach(function (L) {
            svg.append('rect').attr('x', lx).attr('y', mt - 14)
                .attr('width', 10).attr('height', 10).attr('fill', L[2]);
            svg.append('text').attr('x', lx + 13).attr('y', mt - 5)
                .attr('font-size', 11).attr('fill', '#333').text(L[1]);
            lx += 13 + L[1].length * 12 + 14;
        });
        svg.append('text').attr('x', lx + 4).attr('y', mt - 5)
            .attr('font-size', 11).attr('fill', '#666')
            .text('実線・濃色=' + a.name + '、破線・淡色=' + b.name);
    }

    window.ClimateCompare = { draw: drawCompare };
})();
