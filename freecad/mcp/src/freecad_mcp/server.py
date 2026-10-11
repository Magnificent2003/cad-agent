import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Literal

try:
    # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    # mcp 2.x moved mcp.server.fastmcp to mcp.server.mcpserver and renamed
    # FastMCP to MCPServer; the API surface used here is unchanged.
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import ImageContent, TextContent

from .freecad_client import FreeCADConnection
from .operations import (
    create_document_operation,
    create_object_operation,
    delete_object_operation,
    edit_object_operation,
    execute_code_async_operation,
    execute_code_headless_operation,
    execute_code_operation,
    get_object_operation,
    get_objects_operation,
    get_parts_list_operation,
    get_async_status_operation,
    get_rpc_status_operation,
    get_view_operation,
    insert_part_from_library_operation,
    list_documents_operation,
    reload_document_operation,
    run_fem_analysis_operation,
)
from .prompt_text import ASSET_CREATION_STRATEGY
from .server_state import ServerState
from .site_tools import run_site_tool


logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("FreeCADMCPserver")
logger.setLevel(logging.INFO)

ViewName = Literal[
    "Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"
]

state = ServerState()


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("FreeCADMCP server starting up")
        try:
            _ = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD on startup")
        except Exception as e:
            logger.warning(f"Could not connect to FreeCAD on startup: {str(e)}")
            logger.warning(
                "Make sure the FreeCAD addon is running before using FreeCAD resources or tools"
            )
        yield {}
    finally:
        if state.freecad_connection:
            logger.info("Disconnecting from FreeCAD on shutdown")
            state.freecad_connection.disconnect()
            state.freecad_connection = None
        logger.info("FreeCADMCP server shut down")


mcp = FastMCP(
    "FreeCADMCP",
    instructions="FreeCAD integration through the Model Context Protocol",
    lifespan=server_lifespan,
)


def get_freecad_connection() -> FreeCADConnection:
    """Get or create a persistent FreeCAD connection"""
    if state.freecad_connection is None:
        state.freecad_connection = FreeCADConnection(host=state.rpc_host, port=state.rpc_port)
        if not state.freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            state.freecad_connection = None
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
    return state.freecad_connection


@mcp.tool(structured_output=False)
def cad_inspect(
    context_path: str = "", path: str = "", handles: list[str] | None = None, limit: int = 30,
) -> list[TextContent | ImageContent]:
    """Inspect an actual DXF for hashes, exact handles, geometry and scene candidates.

    context_path is supplied by the active task plugin and independently checked
    by the worker. path selects an authorized task file; empty uses the input.
    handles narrows inspection to exact entity handles; limit bounds candidates.
    Candidates, layer names and paper scale do not confirm engineering semantics
    or drawing units. Keep unsupported or ambiguous scene facts unknown.
    This tool is useful for evidence gathering, not a mandatory editing pipeline.
    """
    return run_site_tool("inspect", context_path, {"path": path, "handles": handles, "limit": limit})


@mcp.tool(structured_output=False)
def cad_spatial_facts(
    context_path: str = "", path: str = "", boundary_id: str = "", facts_id: str = "",
    category: str = "", ids: list[str] | None = None, offset: int = 0, limit: int = 15,
    section: str = "outlines",
) -> list[TextContent | ImageContent]:
    """从当前DXF识别场界候选内的空间占地：命名建筑、保留候选、从属重复、待定和非建筑。

    本工具直接只读DXF，无需打开FreeCAD图形界面；不使用项目人工确认文件或既有布局。
    首次调用省略path使用当前输入；
    多场界时可用图内boundary_id选择分析范围，该选择不确认法定红线。
    返回本轮facts_id、证据和分类摘要。重复查询用facts_id；category筛选类别，
    ids指定源实例编号时返回原生坐标，offset/limit分页；显示页可能因字节预算缩短，
    按next_offset继续。极大单个几何会说明省略及完整事实位置，不静默丢弃。section=issues查询问题明细，
    默认section=outlines查询轮廓。不要同时指定facts_id和path。
    单位、身份或方向证据不足会明确返回问题，不以模型空间空白推断可用空地。
    施工主轴、局部米坐标与候选占地核查由cad_spatial_constraints接收同一facts_id计算。
    context_path由当前会话自动绑定，无需模型填写；不写CAD、不自动布置全场。
    """
    return run_site_tool("spatial_facts", context_path, {
        "path": path, "boundary_id": boundary_id, "facts_id": facts_id, "category": category,
        "ids": ids, "offset": offset, "limit": limit, "section": section,
    })


@mcp.tool(structured_output=False)
def cad_spatial_constraints(
    facts_id: str = "", context_path: str = "", proposals: list[dict] | None = None,
    obstacle_offset: int = 0, obstacle_limit: int = 12,
    report_id: str = "", proposal_id: str = "", offset: int = 0, limit: int = 15,
) -> list[TextContent | ImageContent]:
    """计算当前图纸的施工参照轴及候选占地约束，不画道路或自动布置整图。

    首次传cad_spatial_facts返回的facts_id；不传proposals时获取单位、主轴、
    局部米制框架和占地摘要。obstacle_offset为非负偏移，obstacle_limit为1～30；两者仅控制摘要页，
    全部独立占地始终参与计算，不依赖FreeCAD图形界面。
    proposals最多50项：{id,name,role:'facility'|'road'|'coverage',
    position_m:[局部x,局部y],width_m,depth_m,angle_deg:局部角度,clearance_m:净距}；
    position_m是旋转前矩形左下角，兼作旋转锚点，不是中心；angle_deg围绕该点逆时针旋转。
    或用vertices_m提供完整多边形。检查全轮廓越界、交叠、完全包含、净距及轴角偏差。
    返回placement.anchor_m、实际centroid_m/centroid_wcs；矩形corner_points_wcs为含闭合末点的5个角点。
    measurements给出实际越界面积、距场界净距、轴角误差及两类轮廓的最近对象和距离。
    报告坐标、角点、净距直接引用这些回传值，不自行口算或调用GUI再次换算；不能把anchor说成中心。
    一次提交的列表表示同时存在的设施，会检查相互碰撞；同一设施的互斥备选位置须分别检查，
    不能因备选方案彼此相交就认定该位置撞了已有设施。默认返回report_id、全部候选状态和问题计数、
    少量问题样本；完整结果落盘，不因输出摘要而减少检查。
    查看明细时只传report_id，可选proposal_id定位一个候选，再用offset/limit按记录分页；
    使用返回的next_offset继续，显示页可能因字节预算缩短。无proposal_id时读取全报告。
    超长单项返回json_text_fragment，按相同path和fragment_index拼接text后解析。
    读取报告不会累计候选，也不会重新计算。修改多个候选后必须用facts_id提交当前完整集合；
    单独提交新增项不会检查未提交的旧候选。也可保存CAD后重新提取A；改图会使旧facts/report失效。
    coverage不作为实体占地，也不表示已经验算吊装覆盖；证据采用与单位冲突明确记录，
    不伪称人工确认。无冲突不等于规范验收或文件已生成，不能替代保存后cad_review。
    context_path由当前会话自动绑定，无需模型填写。
    """
    return run_site_tool("spatial_constraints", context_path, {
        "facts_id": facts_id, "proposals": proposals,
        "obstacle_offset": obstacle_offset, "obstacle_limit": obstacle_limit,
        "report_id": report_id, "proposal_id": proposal_id, "offset": offset, "limit": limit,
    })


@mcp.tool(structured_output=False)
def cad_site_rules(
    context_path: str = "", facts_id: str = "", overrides: dict | None = None,
) -> list[TextContent | ImageContent]:
    """读取中文施工场布规则；传当前facts_id可计算同图面积任务书，不布置或写CAD。

    缺少用户明确面积时，以当前候选场界面积按配置估算道路及六类设施：钢筋、模板、
    周转材料、砌块/装修、办公、设备。无砂石、塔吊或提升设备的默认布置。
    比例和路宽为可调整研究假设，不是规范。配置为cad_domain/site_layout_rules.yaml。
    overrides可给{facilities:{设施id:{area_m2:平方米}}}、road:{width_m:米}等通用覆盖，
    调整须来自当前任务或说明采用理由，不能隐瞒面积缺额。返回面积预算和保留/主要待定
    占地的并集量测，不把未分类区域当空地。无需模型读取项目文件或记忆历史布局。
    布置位置、分区和道路仍由模型决定，用空间约束及实际成果复核验证。
    """
    return run_site_tool("site_rules", context_path, {"facts_id": facts_id, "overrides": overrides})


@mcp.tool(structured_output=False)
def cad_layout(
    context_path: str = "", source: str = "", target: str = "",
    facts: dict | None = None, components: list[dict] | None = None,
) -> list[TextContent | ImageContent]:
    """按模型给出的设施/道路位置写入可编辑DXF，修改后必须cad_review检查实际文件。

    新空间事实格式：facts={schema:'spatial-evidence/1',facts_id:当前原始输入的事实编号,
    adopted_by:'agent',assumptions:[采用工作单位/场界/暂定占地的中文依据],clearance_m:净距}。
    该约定保留机器证据身份，不需要伪造人工confirmed；从原图解析块内实例，暂避主要待定。
    source省略为本轮输入；target为本轮输出或scratch的新DXF，不覆盖输入或source。
    不含自动整图规划，模型须明确每项位置。components列表：
    矩形{id,name,kind:'rectangle',width_m,depth_m,position_m:[局部左下角x,y],angle_deg:局部角度}；
    道路{id,name,kind:'road',width_m,points_m:[[局部中心线x,y],...]}。
    所有尺寸和局部位置为米，通过证据框架映回原CAD，不旋转底图。操作支持add/move/remove，
    move/remove要匹配唯一id或handle。多次编辑source可为上次已保存成果，facts仍绑定原始输入。
    道路完整宽度走廊也参加越界/占地检查，主轴正交；不能把仅检查中心线当道路可行。
    未通过时不发布新文件，读具体冲突调整后再试；通过表示机器事实下的几何检查，不是规范验收。
    旧source_sha256+confirmed+原生handles事实格式仍支持，不可把两套字段拼接或降级复核。
    """
    return run_site_tool("layout", context_path, {
        "source": source, "target": target, "facts": facts, "components": components,
    })


@mcp.tool(structured_output=False)
def cad_review(
    context_path: str = "", path: str = "", profile: Literal["general", "site_layout"] = "general",
    facts: dict | None = None, components: list[dict] | None = None,
    allowed_changes: list[str] | None = None, render: bool = True,
) -> list[TextContent | ImageContent]:
    """回读实际保存的DXF，检查本次几何和原图保留，并提供同源PNG反馈。

    场布使用profile='site_layout'。cad_layout保存了事实与实体清单时可只传path和render=True。
    自主Python写图时提供同样的spatial-evidence/1 facts及实际实体清单components：
    [{id,role:'facility'|'road',handle:实际新增实体句柄,width_m,depth_m:矩形深度,label_handle:可选}]。
    清单不是计划坐标，检查器从保存DXF量测，所有新增占地都需申报。纯标注/覆盖声明role=annotation
    或coverage和实际句柄，不得将占地设施标作标注绕过检查。
    新事实保留机器推定/工作假设，检查通过也不证明施工规范、全部建筑已识别或空地完全可靠。
    普通编辑可用general和allowed_changes列出准许修改/删除的原生handle；场布不能降级general。
    原图、检查、图片按文件哈希绑定，后续改图需要重查。render=True后必须读取报告并查看预览；
    图片发送不代表模型看懂，图片不可用应说明。后端发布前还独立回读；纯分析无需创建文件复核。
    context_path由当前会话绑定，path在本轮输出或scratch范围。最后只交付本轮output内的最终文件。
    """
    return run_site_tool("review", context_path, {
        "path": path, "profile": profile, "facts": facts, "components": components,
        "allowed_changes": allowed_changes, "render": render,
    })


@mcp.tool(structured_output=False)
def create_document(ctx: Context, name: str) -> list[TextContent]:
    """Create a new document in FreeCAD.

    Args:
        name: The name of the document to create.

    Returns:
        A message indicating the success or failure of the document creation.

    Examples:
        If you want to create a document named "MyDocument", you can use the following data.
        ```json
        {
            "name": "MyDocument"
        }
        ```
    """
    return create_document_operation(get_freecad_connection(), name)


@mcp.tool(structured_output=False)
def create_object(
    ctx: Context,
    doc_name: str,
    obj_type: str,
    obj_name: str,
    analysis_name: str | None = None,
    obj_properties: dict[str, Any] = None,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Create a new object in FreeCAD.
    Object type is starts with "Part::" or "Draft::" or "PartDesign::" or "Fem::".

    Args:
        doc_name: The name of the document to create the object in.
        obj_type: The type of the object to create (e.g. 'Part::Box', 'Part::Cylinder', 'Draft::Circle', 'PartDesign::Body', etc.).
        obj_name: The name of the object to create.
        obj_properties: The properties of the object to create.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the object creation and a screenshot of the object.

    Examples:
        If you want to create a cylinder with a height of 30 and a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCylinder",
            "obj_name": "Cylinder",
            "obj_type": "Part::Cylinder",
            "obj_properties": {
                "Height": 30,
                "Radius": 10,
                "Placement": {
                    "Base": {
                        "x": 10,
                        "y": 10,
                        "z": 0
                    },
                    "Rotation": {
                        "Axis": {
                            "x": 0,
                            "y": 0,
                            "z": 1
                        },
                        "Angle": 45
                    }
                },
                "ViewObject": {
                    "ShapeColor": [0.5, 0.5, 0.5, 1.0]
                }
            }
        }
        ```

        If you want to create a circle with a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCircle",
            "obj_name": "Circle",
            "obj_type": "Draft::Circle",
        }
        ```

        If you want to create a FEM analysis, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemAnalysis",
            "obj_type": "Fem::AnalysisPython",
        }
        ```

        If you want to create a FEM constraint, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMConstraint",
            "obj_name": "FemConstraint",
            "obj_type": "Fem::ConstraintFixed",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "References": [
                    {
                        "object_name": "MyObject",
                        "face": "Face1"
                    }
                ]
            }
        }
        ```

        If you want to create a FEM mechanical material, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemMechanicalMaterial",
            "obj_type": "Fem::MaterialCommon",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Material": {
                    "Name": "MyMaterial",
                    "Density": "7900 kg/m^3",
                    "YoungModulus": "210 GPa",
                    "PoissonRatio": 0.3
                }
            }
        }
        ```

        If you want to create a FEM mesh, you can use the following data.
        The `Shape` property is required (legacy `Part` is also accepted).
        On FreeCAD 1.x the size limits are `CharacteristicLengthMax/Min`;
        the legacy `ElementSizeMax/Min` keys are also accepted.
        ```json
        {
            "doc_name": "MyFEMMesh",
            "obj_name": "FemMesh",
            "obj_type": "Fem::FemMeshGmsh",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Shape": "MyObject",
                "CharacteristicLengthMax": 10,
                "CharacteristicLengthMin": 0.1
            }
        }
        ```
    """
    return create_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_type,
        obj_name,
        analysis_name,
        obj_properties,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def edit_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    obj_properties: dict[str, Any],
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Edit an object in FreeCAD.
    This tool is used when the `create_object` tool cannot handle the object creation.

    Args:
        doc_name: The name of the document to edit the object in.
        obj_name: The name of the object to edit.
        obj_properties: The properties of the object to edit.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the object editing and a screenshot of the object.
    """
    return edit_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        obj_properties,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def delete_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Delete an object in FreeCAD.

    Args:
        doc_name: The name of the document to delete the object from.
        obj_name: The name of the object to delete.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the object deletion and a screenshot of the object.
    """
    return delete_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def execute_code_async(ctx: Context, code: str) -> list[TextContent]:
    """Execute Python code in FreeCAD without waiting for completion.

    Use this ONLY for long-running background computations that do NOT touch the
    FreeCAD GUI or mutate the FreeCAD document tree directly.

    This tool runs the submitted code in a background thread and returns
    immediately. Because it does not run on FreeCAD's main GUI thread, the code
    must NOT directly call FreeCADGui APIs, manipulate the active view or
    selection, create or edit document objects, change object properties, call
    doc.recompute(), or save documents. FreeCAD documents and the Coin3D
    scenegraph are not thread-safe: writing to them from this thread races the
    GUI thread and can wedge FreeCAD's event loop, after which the RPC server
    stops responding entirely and FreeCAD must be restarted.

    Every document or view write must instead be handed to the GUI thread through
    the injected commit() helper:

        commit(fn, timeout=120) -> fn's return value

    Scripts share a live namespace. Saved functions can use commit() in later
    async calls; calling it from execute_code or a GUI callback raises immediately.
    Coordinate concurrent scripts that intentionally modify the same variables.

    commit() queues fn on the GUI thread, waits for it, and raises RuntimeError if
    dispatch fails or times out. Example:

        fused = base.fuse(addition).removeSplitter()   # slow, safe in background

        def apply():                                   # runs on the GUI thread
            obj.Shape = fused
            doc.recompute()

        commit(apply)

    For code that is not dominated by heavy geometry computation, use execute_code
    instead. execute_code runs entirely on the FreeCAD GUI thread and is the safe
    default for normal FreeCAD automation.

    Use execute_code_async only when the heavy part is long-running OCCT geometry
    (e.g. fuse/cut/loft on already-fetched shapes) or other CPU-bound computation
    that would exceed execute_code's 90 s GUI-thread budget.

    Typical usage pattern:
    1. Fetch shapes into module-level variables first (via execute_code).
    2. Run the heavy computation via execute_code_async.
    3. Apply the result inside commit(), or store it in a module-level Python
       variable (not in the FreeCAD document) for a later execute_code call.

    Performance note: boolean operations against shapes with many faces (e.g. a
    ribbed lid) are expensive. Fuse the additions together first, then apply a
    single boolean against the heavy shape, and avoid doc.recompute() unless the
    dependency graph really needs it.

    Args:
        code: Background-safe Python code to execute. Use commit(fn) for all
            document and view writes.

    Returns:
        A message with the job_id of the started background execution.
    """
    return execute_code_async_operation(get_freecad_connection(), code)


@mcp.tool(structured_output=False)
def execute_code_headless(ctx: Context, code: str, timeout: float = 600) -> list[TextContent]:
    """Run a FreeCAD Python script in a separate headless `freecadcmd` process.

    Use this for OCCT work that can crash or block FreeCAD: helical threads
    (makeHelix + makePipeShell), lofts and sweeps, booleans with many or
    B-spline tools, long parametric rebuilds. A native OpenCascade crash
    here only kills the helper process; the GUI and its open documents
    survive, and the tool reports the crash signal and the script's output.

    The script runs on the MCP server machine, independently of --host, in a
    fresh process without GUI: import FreeCAD/Part
    yourself, open documents from disk (FreeCAD.openDocument(path)), save
    results with doc.save()/saveAs() or Shape.exportBrep(). Nothing from the
    execute_code namespace is available. Print progress to stdout; it is
    returned when the process ends. After the script saved a .FCStd that is
    open in the GUI, call reload_document(doc_name) to show the result.

    Args:
        code: Complete Python script for freecadcmd.
        timeout: Positive finite seconds to wait before killing the process
            (default 600). Partial output is preserved on timeout.

    Returns:
        Exit status, crash/timeout diagnosis and the script's printed output.
    """
    return execute_code_headless_operation(state.freecadcmd, code, timeout)


@mcp.tool(structured_output=False)
def get_async_status(ctx: Context, job_id: str = "") -> list[TextContent]:
    """Report the state of background jobs started by execute_code_async.

    Does not use the FreeCAD GUI thread, so it answers even while a job runs.

    Args:
        job_id: The id returned by execute_code_async. Empty lists all running
            jobs and up to 20 recently completed jobs.

    Returns:
        For one job: its state (running/done/failed), the error and traceback
        when it failed. History is held in memory until FreeCAD exits.
    """
    return get_async_status_operation(get_freecad_connection(), job_id)


@mcp.tool(structured_output=False)
def execute_code(
    ctx: Context,
    code: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
    timeout: float | None = None,
) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python code in FreeCAD.

    Args:
        code: The Python code to execute.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when the code does not change the model's
            appearance, e.g. analytical or computational scripts whose result is
            printed output, or intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.
        timeout: Positive finite seconds for each of the queue and GUI execution
            budgets, overriding the 90 s default for this call (capped at 1800).
            Raise it for slow work that must
            run on the GUI thread, such as importing or exporting a large STEP
            assembly. Without it the call reports a timeout while the task keeps
            running, and the result is lost even though the work finishes. Prefer
            execute_code_async for heavy pure-geometry work that touches neither
            the document nor the GUI.

    Returns:
        A message indicating the success or failure of the code execution, the output of the code execution, and a screenshot of the object.
    """
    return execute_code_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        code,
        include_screenshot,
        view_name,
        timeout,
    )


@mcp.tool(structured_output=False)
def get_view(
    ctx: Context,
    view_name: ViewName,
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> list[ImageContent | TextContent]:
    """Get a screenshot of the active view.

    Args:
        view_name: The name of the view to get the screenshot of.
        The following views are available:
        - "Isometric"
        - "Front"
        - "Top"
        - "Right"
        - "Back"
        - "Left"
        - "Bottom"
        - "Dimetric"
        - "Trimetric"
        width: The width of the screenshot in pixels. If not specified, uses the viewport width.
        height: The height of the screenshot in pixels. If not specified, uses the viewport height.
        focus_object: The name of the object to focus on. If not specified, fits all objects in the view.

    Returns:
        A screenshot of the active view.
    """
    return get_view_operation(get_freecad_connection(), view_name, width, height, focus_object)


@mcp.tool(structured_output=False)
def insert_part_from_library(
    ctx: Context,
    relative_path: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Insert a part from the parts library addon.

    Args:
        relative_path: The relative path of the part to insert.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the part insertion and a screenshot of the object.
    """
    return insert_part_from_library_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        relative_path,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def get_objects(
    ctx: Context,
    doc_name: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Get all objects in a document.
    You can use this tool to get the objects in a document to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the objects from.
        include_screenshot: Whether to return a screenshot of the document (default True).
            Set to False to save tokens when only the object data is needed.
        view_name: The view orientation of the returned screenshot (default "Isometric").

    Returns:
        A list of objects in the document and a screenshot of the document.
    """
    return get_objects_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def get_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Get an object from a document.
    You can use this tool to get the properties of an object to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the object from.
        obj_name: The name of the object to get.
        include_screenshot: Whether to return a screenshot of the document (default True).
            Set to False to save tokens when only the object data is needed.
        view_name: The view orientation of the returned screenshot (default "Isometric").

    Returns:
        The object and a screenshot of the object.
    """
    return get_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def get_parts_list(ctx: Context) -> list[TextContent]:
    """Get the list of parts in the parts library addon.
    """
    return get_parts_list_operation(get_freecad_connection())


@mcp.tool(structured_output=False)
def reload_document(ctx: Context, doc_name: str) -> list[TextContent]:
    """Close and re-open a document to pick up external file changes.

    Use this AFTER the document's .FCStd file has been modified by
    something outside of FreeCAD's GUI process — for example, a
    headless `freecadcmd` script that edited and saved the file. The
    open GUI document is otherwise unaware of on-disk changes; this
    tool closes the stale in-memory copy and reopens the file from
    disk so the GUI shows current geometry.

    Args:
        doc_name: The name of the open document to reload. Must match
            the name shown by ``list_documents``.

    Returns:
        A message confirming the document was reloaded, or describing
        the failure (document not loaded, no associated file, etc).

    Examples:
        ```json
        {
            "doc_name": "chassis"
        }
        ```
    """
    return reload_document_operation(get_freecad_connection(), doc_name)


@mcp.tool(structured_output=False)
def list_documents(ctx: Context) -> list[TextContent]:
    """Get the list of open documents in FreeCAD.

    Returns:
        A list of document names.
    """
    return list_documents_operation(get_freecad_connection())


@mcp.tool(structured_output=False)
def get_rpc_status(ctx: Context) -> list[TextContent]:
    """Get RPC and FreeCAD GUI-dispatch health.

    This tool does not use FreeCAD's GUI thread, so it remains available after
    a GUI operation times out. A ``stuck`` state identifies the operation that
    is still running and indicates that FreeCAD may need to be restarted.
    """
    return get_rpc_status_operation(get_freecad_connection())


@mcp.tool(structured_output=False)
def run_fem_analysis(
    ctx: Context,
    doc_name: str,
    analysis_name: str,
    timeout: int = 600,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Run the CalculiX solver on an existing Fem::FemAnalysis container and return summary results.

    Prerequisites in the document:
    - A Part-derived solid (e.g. Part::Box, PartDesign::Body) acting as the geometry.
    - A Fem::AnalysisPython container created via `create_object`.
    - A Fem::MaterialCommon assigned to the geometry, added to the analysis.
    - A Fem::FemMeshGmsh referencing the geometry, added to the analysis (the
      mesh is generated automatically when created via `create_object`).
    - At least one Fem::ConstraintFixed and one Fem::ConstraintForce (or
      ConstraintPressure) bound to faces of the geometry, added to the analysis.

    A SolverCcxTools is auto-created if the analysis has none.

    The solver runs synchronously on the FreeCAD GUI thread and blocks all
    other RPC calls for its duration; do not fan out parallel requests.

    Returns max von Mises stress (MPa), max/min displacement (mm), node count,
    and the working directory CalculiX wrote to. On failure, returns the
    prerequisite-check or solver error along with the working directory for
    triage.

    Args:
        doc_name: Name of the FreeCAD document.
        analysis_name: Name of the Fem::AnalysisPython object.
        timeout: Seconds to wait for the solver (default 600).
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when only the numeric results are needed.
        view_name: The view orientation of the returned screenshot (default "Isometric").
    """
    return run_fem_analysis_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        analysis_name,
        timeout,
        include_screenshot,
        view_name,
    )


@mcp.prompt()
def asset_creation_strategy() -> str:
    return ASSET_CREATION_STRATEGY


def _validate_host(value: str) -> str:
    """Validate that *value* is a valid IP address or hostname.

    Used as the ``type`` callback for the ``--host`` argparse argument.
    Raises ``argparse.ArgumentTypeError`` on invalid input.
    """
    import argparse

    import validators

    if validators.ipv4(value) or validators.ipv6(value) or validators.hostname(value):
        return value
    raise argparse.ArgumentTypeError(
        f"Invalid host: '{value}'. Must be a valid IP address or hostname."
    )


def main():
    """Run the MCP server"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--only-text-feedback", action="store_true", help="Only return text feedback")
    parser.add_argument("--host", type=_validate_host, default="localhost", help="Host address of the FreeCAD RPC server to connect to (default: localhost)")
    parser.add_argument("--port", type=int, default=9875, help="FreeCAD RPC port (default: 9875)")
    parser.add_argument("--freecadcmd", default=None, help="Command that starts headless FreeCAD for execute_code_headless, e.g. 'flatpak run --command=freecadcmd org.freecad.FreeCAD' (default: auto-detect PATH, then Flatpak)")
    args = parser.parse_args()
    state.only_text_feedback = args.only_text_feedback
    state.rpc_host = args.host
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    state.rpc_port = args.port
    from .headless import parse_command
    state.freecadcmd = parse_command(args.freecadcmd)
    logger.info(f"Only text feedback: {state.only_text_feedback}")
    logger.info(f"Connecting to FreeCAD RPC server at: {state.rpc_host}:{state.rpc_port}")
    mcp.run()
