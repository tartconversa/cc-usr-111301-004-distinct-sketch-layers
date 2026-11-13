# 可替换叶分区的去重草图聚合层

本项目保存虚构算力设施的仓库源表、湖文件与Python读取工具。现有工具仅加载输入并显示记录数，不执行事实加工、目录提交、汇总或联接。所有设备、组织和金额均为合成。

## 字段与单位

leaf_devices保存每叶设备键与revision，sketches给固定哈希、p和父叶引用，不包含已计算草图或估计值。设备键均为虚构。

material-index.json记录全部输入内容哈希；fixtures保存原始资料与配置，计算输出可放.workspace-output，原始资料不应被覆盖。

## 运行已有读取器

Python 3.11以上，标准库即可，不需安装第三方依赖。

    python -m src.sketch_reader fixtures/leaf_devices.json
    python -m compileall -q src tests
    python -m unittest discover -s tests -v

## 固定容器

    docker build -t distinct-sketch-layers:local .
    docker run --rm distinct-sketch-layers:local

容器镜像以完整digest固定，默认仅检查输入契约；实现新增分析功能所需依赖由开发者明确登记。

## HLL 近似去重聚合层

`src/hll.py`（核心）、`src/sketch_store.py`（叶/父存储与缓存）、`src/sketch_layer.py`（CLI）仅用标准库。

### 哈希与寄存器规范（固定）

* 设备键按 UTF-8 字节做 SHA256，取前 8 字节按大端序解释为 64 位整数；哈希版本 `sha256_utf8_first64_bigendian` 与精度 `p` 必须写入每个草图头。
* 高 `p` 位选寄存器（m=2^p，正式 p 为 4..16）；剩余 64-p 位取前导零数加 1 为 rank，余串全零时 rank=65-p。
* 合并是同下标寄存器逐位 max，只允许相同 p 与相同哈希版本；不同 p 不做对齐换算，直接抛 `SketchHeaderError`。m=4（p=2）只用于 max 代数单元核对，不是正式精度。
* 估计：`raw = alpha_m·m²/Σ2^-Rj`，alpha_m 对 m=16/32/64 取 0.673/0.697/0.709，其余为 0.7213/(1+1.079/m)；当 `raw ≤ 2.5m` 且零寄存器 V>0 时切换 linear counting `m·ln(m/V)`。64 位哈希不做经典大范围修正。~1.04/√m 是渐近统计口径，**不保证每个单次结果都落在理论相对误差内**；空集显式估计为 0。

### 叶与父的缓存语义

* 叶是不可变版本：`leaf_<分区>_rev<修订号>.hll`，更正以新 revision 的新文件表达；叶头另存精确设备键集合与精确基数，供小样本核对。
* 父是可重建缓存：缓存键是规范化（去重、排序）叶引用集合的哈希，引用身份为 (分区, revision, 叶文件 sha256)，与路径无关。重复叶引用在规范化阶段折叠，不双计；同路径换内容后摘要失配，旧缓存不会被复用。
* 叶替换后**禁止对父寄存器做减法**：修订从当前仍有效的全部叶重新 merge，经同目录临时文件 + `os.replace` 原子进入目录；写入中断时旧父文件保持完整可读。
* 父结果同时给出叶集合摘要、原始估计、校正估计、零寄存器数与是否命中缓存。精确并集与近似估计可通过 `exact` 命令并列核对（两叶 {a,b}∪{b,c}=3 而非 4；第二叶换成 {a} 后并集为 2）。
* 落盘格式：`HLL1 | 头长度(4B大端) | JSON头 | m字节寄存器`，尾随字节、魔数错误、头中 p/哈希版本缺失均拒绝解析。

### 命令行用法（输出到 .workspace-output，不覆盖 fixtures）

    python -m src.sketch_layer commit-leaf fixtures/leaf_devices.json day1 1 --store-dir .workspace-output/store
    python -m src.sketch_layer commit-leaf fixtures/leaf_devices.json day2 1 --store-dir .workspace-output/store
    python -m src.sketch_layer parent week1 day1@1 day2@1 --store-dir .workspace-output/store
    python -m src.sketch_layer exact  week1 day1@1 day2@1 --store-dir .workspace-output/store
    python -m src.sketch_layer commit-leaf fixtures/leaf_devices.json day2 2 --store-dir .workspace-output/store
    python -m src.sketch_layer revise week1 day1@1 day2@2 --store-dir .workspace-output/store

设备键全部为合成短键，测试含防回归断言，禁止引入真实用户标识。
