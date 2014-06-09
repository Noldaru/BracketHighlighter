import sublime
import sublime_plugin
from os.path import basename, join
from time import time, sleep
import _thread as thread
import traceback
import BracketHighlighter.ure as ure
import BracketHighlighter.bh_plugin as bh_plugin
import BracketHighlighter.bh_search as bh_search
import BracketHighlighter.bh_regions as bh_regions
import BracketHighlighter.bh_logging as bh_logging

bh_match = None

BH_MATCH_TYPE_NONE = 0
BH_MATCH_TYPE_SELECTION = 1
BH_MATCH_TYPE_EDIT = 2
GLOBAL_ENABLE = True
HIGH_VISIBILITY = False


class BracketDefinition(object):
    """
    Normal bracket definition.
    """

    def __init__(self, bracket):
        """
        Setup the bracket object by reading the passed in dictionary.
        """

        self.name = bracket["name"]
        self.style = bracket.get("style", "default")
        self.compare = bracket.get("compare")
        sub_search = bracket.get("find_in_sub_search", "false")
        self.find_in_sub_search_only = sub_search == "only"
        self.find_in_sub_search = sub_search == "true" or self.find_in_sub_search_only
        self.post_match = bracket.get("post_match")
        self.validate = bracket.get("validate")
        self.scope_exclude_exceptions = bracket.get("scope_exclude_exceptions", [])
        self.scope_exclude = bracket.get("scope_exclude", [])
        self.ignore_string_escape = bracket.get("ignore_string_escape", False)


class ScopeDefinition(object):
    """
    Scope bracket definition.
    """

    def __init__(self, bracket):
        """
        Setup the bracket object by reading the passed in dictionary.
        """

        self.style = bracket.get("style", "default")
        self.open = ure.compile("\\A" + bracket.get("open", "."), ure.MULTILINE | ure.IGNORECASE)
        self.close = ure.compile(bracket.get("close", ".") + "\\Z", ure.MULTILINE | ure.IGNORECASE)
        self.name = bracket["name"]
        sub_search = bracket.get("sub_bracket_search", "false")
        self.sub_search_only = sub_search == "only"
        self.sub_search = self.sub_search_only is True or sub_search == "true"
        self.compare = bracket.get("compare")
        self.post_match = bracket.get("post_match")
        self.validate = bracket.get("validate")
        self.scopes = bracket["scopes"]


class BhEventMgr(object):
    """
    Object to manage when bracket events should be launched.
    """

    @classmethod
    def load(cls):
        """
        Initialize variables for determining
        when to initiate a bracket matching event.
        """

        cls.wait_time = 0.12
        cls.time = time()
        cls.modified = False
        cls.type = BH_MATCH_TYPE_SELECTION
        cls.ignore_all = False

BhEventMgr.load()


class BhThreadMgr(object):
    """
    Object to help track when a new thread needs to be started.
    """

    restart = False


class BhToggleStringEscapeModeCommand(sublime_plugin.TextCommand):
    """
    Toggle between regex escape and
    string escape for brackets in strings.
    """

    def run(self, edit):
        default_mode = sublime.load_settings("bh_core.sublime-settings").get('bracket_string_escape_mode', 'string')
        if self.view.settings().get('bracket_string_escape_mode', default_mode) == "regex":
            self.view.settings().set('bracket_string_escape_mode', "string")
            sublime.status_message("Bracket String Escape Mode: string")
        else:
            self.view.settings().set('bracket_string_escape_mode', "regex")
            sublime.status_message("Bracket String Escape Mode: regex")


class BhShowStringEscapeModeCommand(sublime_plugin.TextCommand):
    """
    Shoe current string escape mode for sub brackets in strings.
    """

    def run(self, edit):
        default_mode = sublime.load_settings("BracketHighlighter.sublime-settings").get('bracket_string_escape_mode', 'string')
        sublime.status_message("Bracket String Escape Mode: %s" % self.view.settings().get('bracket_string_escape_mode', default_mode))


class BhToggleHighVisibilityCommand(sublime_plugin.ApplicationCommand):
    """
    Toggle a high visibility mode that
    highlights the entire bracket extent.
    """

    def run(self):
        global HIGH_VISIBILITY
        HIGH_VISIBILITY = not HIGH_VISIBILITY


class BhToggleEnableCommand(sublime_plugin.ApplicationCommand):
    """
    Toggle global enable for BracketHighlighter.
    """

    def run(self):
        global GLOBAL_ENABLE
        GLOBAL_ENABLE = not GLOBAL_ENABLE


class BhKeyCommand(sublime_plugin.WindowCommand):
    """
    Command to process shortcuts, menu calls, and command palette calls.
    This is how BhCore is called with different options.
    """

    def run(self, threshold=True, lines=False, adjacent=False, no_outside_adj=False, ignore={}, plugin={}):
        # Override events
        BhEventMgr.ignore_all = True
        BhEventMgr.modified = False
        self.bh = BhCore(
            threshold,
            lines,
            adjacent,
            no_outside_adj,
            ignore,
            plugin,
            True
        )
        self.view = self.window.active_view()
        sublime.set_timeout(self.execute, 100)

    def execute(self):
        bh_logging.bh_debug("Key Event")
        self.bh.match(self.view)
        BhEventMgr.ignore_all = False
        BhEventMgr.time = time()


class BhCore(object):
    """
    Bracket matching class.
    """
    plugin_reload = False

    def __init__(
        self, override_thresh=False, count_lines=False,
        adj_only=None, no_outside_adj=False,
        ignore={}, plugin={}, keycommand=False
    ):
        """
        Load settings and setup reload events if settings changes.
        """

        self.settings = sublime.load_settings("bh_core.sublime-settings")
        self.keycommand = keycommand
        if not keycommand:
            self.settings.clear_on_change('reload')
            self.settings.add_on_change('reload', self.setup)
        self.setup(override_thresh, count_lines, adj_only, no_outside_adj, ignore, plugin)

    def setup(self, override_thresh=False, count_lines=False, adj_only=None, no_outside_adj=False, ignore={}, plugin={}):
        """
        Initialize class settings from settings file and inputs.
        """

        # Init view params
        self.last_id_view = None
        self.last_id_sel = None
        self.view_tracker = (None, None)
        self.ignore_threshold = override_thresh or bool(self.settings.get("ignore_threshold", False))
        self.adj_only = adj_only if adj_only is not None else bool(self.settings.get("match_only_adjacent", False))
        self.auto_selection_threshold = int(self.settings.get("auto_selection_threshold", 10))
        self.default_string_escape_mode = str(self.settings.get('bracket_string_escape_mode', "string"))

        # Init bracket objects
        self.bracket_types = self.settings.get("brackets", []) + self.settings.get("user_brackets", [])
        self.scope_types = self.settings.get("scope_brackets", []) + self.settings.get("user_scope_brackets", [])
        self.bracket_out_adj = False if no_outside_adj else self.settings.get("bracket_outside_adjacent", False)

        # Init selection params
        self.use_selection_threshold = True
        self.selection_threshold = int(self.settings.get("search_threshold", 5000))
        self.loaded_modules = set([])

        # Init plugin
        alter_select = False
        self.plugin = None
        self.transform = set([])
        if 'command' in plugin:
            self.plugin = bh_plugin.BracketPlugin(plugin, self.loaded_modules)
            alter_select = True
            if 'type' in plugin:
                for t in plugin["type"]:
                    self.transform.add(t)

        # Region selection, highlight, managment
        self.regions = bh_regions.BhRegion(alter_select, count_lines)

    def init_brackets(self, language):
        """
        Initialize bracket match definition objects from settings file.
        """

        self.find_regex = []
        self.sub_find_regex = []
        self.index_open = {}
        self.index_close = {}
        self.brackets = []
        self.scopes = []
        self.view_tracker = (language, self.view.id())
        self.enabled = False
        self.check_compare = False
        self.check_validate = False
        self.check_post_match = False

        loaded_modules = self.loaded_modules.copy()

        self.parse_bracket_definition(language, loaded_modules)
        self.parse_scope_definition(language, loaded_modules)

        if len(self.brackets):
            bh_logging.bh_debug(
                "Search patterns: (%s)\n" % ','.join([b.name for b in self.brackets]) +
                "    search (opening|closing):     (?:%s)\n" % '|'.join(self.find_regex) +
                "    sub-search (opening|closing): (?:%s)" % '|'.join(self.sub_find_regex)
            )
            self.sub_pattern = ure.compile("(?:%s)" % '|'.join(self.sub_find_regex), ure.MULTILINE | ure.IGNORECASE)
            self.pattern = ure.compile("(?:%s)" % '|'.join(self.find_regex), ure.MULTILINE | ure.IGNORECASE)
            self.enabled = True

    def parse_bracket_definition(self, language, loaded_modules):
        """
        Parse the bracket defintion
        """

        for params in self.bracket_types:
            if bh_search.is_valid_definition(params, language):
                try:
                    bh_plugin.load_modules(params, loaded_modules)
                    entry = BracketDefinition(params)
                    if not self.check_compare and entry.compare is not None:
                        self.check_compare = True
                    if not self.check_validate and entry.validate is not None:
                        self.check_validate = True
                    if not self.check_post_match and entry.post_match is not None:
                        self.check_post_match = True
                    self.brackets.append(entry)
                    if not entry.find_in_sub_search_only:
                        self.find_regex.append(params["open"])
                        self.find_regex.append(params["close"])
                    else:
                        self.find_regex.append(r"([^\s\S])")
                        self.find_regex.append(r"([^\s\S])")

                    if entry.find_in_sub_search:
                        self.sub_find_regex.append(params["open"])
                        self.sub_find_regex.append(params["close"])
                    else:
                        self.sub_find_regex.append(r"([^\s\S])")
                        self.sub_find_regex.append(r"([^\s\S])")
                except Exception as e:
                    bh_logging.bh_log(e)

    def parse_scope_definition(self, language, loaded_modules):
        """
        Parse the scope defintion
        """

        scopes = {}
        scope_count = 0
        for params in self.scope_types:
            if bh_search.is_valid_definition(params, language):
                try:
                    bh_plugin.load_modules(params, loaded_modules)
                    entry = bh_search.ScopeDefinition(params)
                    if not self.check_compare and entry.compare is not None:
                        self.check_compare = True
                    if not self.check_validate and entry.validate is not None:
                        self.check_validate = True
                    if not self.check_post_match and entry.post_match is not None:
                        self.check_post_match = True
                    for x in entry.scopes:
                        if x not in scopes:
                            scopes[x] = scope_count
                            scope_count += 1
                            self.scopes.append({"name": x, "brackets": [entry]})
                        else:
                            self.scopes[scopes[x]]["brackets"].append(entry)
                    bh_logging.bh_debug("Scope Regex (%s)\n    Opening: %s\n    Closing: %s\n" % (entry.name, entry.open.pattern, entry.close.pattern))
                except Exception as e:
                    bh_logging.bh_log(e)

    def init_match(self, num_sels):
        """
        Reset matching settings for the current view's syntax.
        """

        syntax = self.view.settings().get('syntax')
        language = basename(syntax).replace('.tmLanguage', '').lower() if syntax is not None else "plain text"

        self.regions.reset(self.view, num_sels)

        if language != self.view_tracker[0] or self.view.id() != self.view_tracker[1]:
            self.init_brackets(language)
            self.regions.set_show_unmatched(language)

    def unique(self):
        """
        Check if the current selection(s) is different from the last.
        """

        id_view = self.view.id()
        id_sel = "".join([str(sel.a) for sel in self.view.sel()])
        is_unique = False
        if id_view != self.last_id_view or id_sel != self.last_id_sel:
            self.last_id_view = id_view
            self.last_id_sel = id_sel
            is_unique = True
        return is_unique

    def get_search_bfr(self, sel):
        """
        Read in the view's buffer for scanning for brackets etc.
        """

        # Determine how much of the buffer to search
        view_min = 0
        view_max = self.view.size()
        if not self.ignore_threshold:
            left_delta = sel.a - view_min
            right_delta = view_max - sel.a
            limit = self.selection_threshold / 2
            rpad = limit - left_delta if left_delta < limit else 0
            lpad = limit - right_delta if right_delta < limit else 0
            llimit = limit + lpad
            rlimit = limit + rpad
            self.search_window = (
                sel.a - llimit if left_delta >= llimit else view_min,
                sel.a + rlimit if right_delta >= rlimit else view_max
            )
        else:
            self.search_window = (0, view_max)

        # Search Buffer
        return self.view.substr(sublime.Region(0, view_max))

    def run_plugin(self, name, left, right, regions):
        """
        Run a bracket plugin.
        """

        lbracket = bh_plugin.BracketRegion(left.begin, left.end)
        rbracket = bh_plugin.BracketRegion(right.begin, right.end)
        nobracket = False

        if (
            ("__all__" in self.transform or name in self.transform) and
            self.plugin is not None and
            self.plugin.is_enabled()
        ):
            lbracket, rbracket, regions, nobracket = self.plugin.run_command(self.view, name, lbracket, rbracket, regions)
            left = left.move(lbracket.begin, lbracket.end) if lbracket is not None else None
            right = right.move(rbracket.begin, rbracket.end) if rbracket is not None else None
        return left, right, regions, nobracket

    def validate(self, b, bracket_type, bfr, scope_bracket=False):
        """
        Validate bracket.
        """

        match = True

        if not self.check_validate:
            return match

        bracket = self.scopes[b.scope]["brackets"][b.type] if scope_bracket else self.brackets[b.type]
        if bracket.validate is not None:
            try:
                match = bracket.validate(
                    bracket.name,
                    bh_plugin.BracketRegion(b.begin, b.end),
                    bracket_type,
                    bfr
                )
            except:
                bh_logging.bh_log("Plugin Bracket Find Error:\n%s" % str(traceback.format_exc()))
        return match

    def compare(self, first, second, bfr, scope_bracket=False):
        """
        Compare brackets.  This function allows bracket plugins to add aditional logic.
        """

        if scope_bracket:
            match = first is not None and second is not None
        else:
            match = first.type == second.type

        if not self.check_compare:
            return match

        if match:
            bracket = self.scopes[first.scope]["brackets"][first.type] if scope_bracket else self.brackets[first.type]
            try:
                if bracket.compare is not None and match:
                    match = bracket.compare(
                        bracket.name,
                        bh_plugin.BracketRegion(first.begin, first.end),
                        bh_plugin.BracketRegion(second.begin, second.end),
                        bfr
                    )
            except:
                bh_logging.bh_log("Plugin Compare Error:\n%s" % str(traceback.format_exc()))
        return match

    def post_match(self, left, right, center, bfr, scope_bracket=False):
        """
        Peform special logic after a match has been made.
        This function allows bracket plugins to add aditional logic.
        """

        if left is not None:
            if scope_bracket:
                bracket = self.scopes[left.scope]["brackets"][left.type]
                bracket_scope = left.scope
            else:
                bracket = self.brackets[left.type]
            bracket_type = left.type
        elif right is not None:
            if scope_bracket:
                bracket = self.scopes[right.scope]["brackets"][right.type]
                bracket_scope = right.scope
            else:
                bracket = self.brackets[right.type]
            bracket_type = right.type
        else:
            return left, right

        self.bracket_style = bracket.style

        if not self.check_post_match:
            return left, right

        if bracket.post_match is not None:
            try:
                lbracket, rbracket, self.bracket_style = bracket.post_match(
                    self.view,
                    bracket.name,
                    bracket.style,
                    bh_plugin.BracketRegion(left.begin, left.end) if left is not None else None,
                    bh_plugin.BracketRegion(right.begin, right.end) if right is not None else None,
                    center,
                    bfr,
                    self.search_window
                )

                if scope_bracket:
                    left = bh_search.ScopeEntry(lbracket.begin, lbracket.end, bracket_scope, bracket_type) if lbracket is not None else None
                    right = bh_search.ScopeEntry(rbracket.begin, rbracket.end, bracket_scope, bracket_type) if rbracket is not None else None
                else:
                    left = bh_search.BracketEntry(lbracket.begin, lbracket.end, bracket_type) if lbracket is not None else None
                    right = bh_search.BracketEntry(rbracket.begin, rbracket.end, bracket_type) if rbracket is not None else None
            except:
                bh_logging.bh_log("Plugin Post Match Error:\n%s" % str(traceback.format_exc()))

        return left, right

    def match(self, view, force_match=True):
        """
        Preform matching brackets surround the selection(s)
        """

        if view is None:
            return

        view.settings().set("BracketHighlighterBusy", True)

        if not GLOBAL_ENABLE:
            for region_key in view.settings().get("bh_regions", []):
                view.erase_regions(region_key)
            view.settings().set("BracketHighlighterBusy", False)
            return

        if self.keycommand:
            BhCore.plugin_reload = True

        if not self.keycommand and BhCore.plugin_reload:
            self.setup()
            BhCore.plugin_reload = False

        # Setup views
        self.view = view
        self.last_view = view

        if self.unique() or force_match:
            # Initialize
            num_sels = len(view.sel())
            self.init_match(num_sels)

            # Nothing to search for
            if not self.enabled:
                view.settings().set("BracketHighlighterBusy", False)
                return

            # Abort if selections are beyond the threshold
            if self.use_selection_threshold and num_sels >= self.selection_threshold:
                self.highlight(view)
                view.settings().set("BracketHighlighterBusy", False)
                return

            multi_select_count = 0
            # Process selections.
            for sel in view.sel():
                if not self.ignore_threshold and multi_select_count >= self.auto_selection_threshold:
                    self.regions.store_sel([sel])
                    multi_select_count += 1
                    continue
                self.recursive_guard = False
                self.bracket_style = None
                bfr = self.get_search_bfr(sel)
                if not self.find_scopes(bfr, sel):
                    self.sub_search_mode = False
                    self.find_matches(bfr, sel)
                multi_select_count += 1

        # Highlight, focus, and display lines etc.
        self.regions.highlight(HIGH_VISIBILITY)

        view.settings().set("BracketHighlighterBusy", False)

    def sub_search(self, sel, search_window, bfr, scope=None):
        """
        Search a scope bracket match for bracekts within.
        """

        self.recursive_guard = True
        bracket = None
        left, right, scope_adj = self.match_brackets(bfr, search_window, sel, scope)

        regions = [sublime.Region(sel.a, sel.b)]

        if left is not None and right is not None:
            bracket = self.brackets[left.type]
            left, right, regions, nobracket = self.run_plugin(bracket.name, left, right, regions)
            if nobracket:
                return True

        # Matched brackets
        if left is not None and right is not None and bracket is not None:
            self.regions.save_complete_regions(left, right, regions, self.bracket_style, HIGH_VISIBILITY)
            return True
        return False

    def find_scopes(self, bfr, sel, adj_dir=-1):
        """
        Find brackets by scope definition.
        """

        # Search buffer
        left, right, bracket, sub_matched = self.match_scope_brackets(bfr, sel, adj_dir)
        if sub_matched:
            return True
        regions = [sublime.Region(sel.a, sel.b)]

        if left is not None and right is not None:
            left, right, regions, _ = self.run_plugin(bracket.name, left, right, regions)
            if left is None and right is None:
                self.regions.store_sel(regions)
                return True

        return self.regions.save_regions(left, right, regions, self.bracket_style, HIGH_VISIBILITY)

    def find_matches(self, bfr, sel):
        """
        Find bracket matches
        """

        bracket = None
        left, right, adj_scope = self.match_brackets(bfr, self.search_window, sel)
        if adj_scope:
            return

        regions = [sublime.Region(sel.a, sel.b)]

        if left is not None and right is not None:
            bracket = self.brackets[left.type]
            left, right, regions, _ = self.run_plugin(bracket.name, left, right, regions)

        if not self.regions.save_regions(left, right, regions, self.bracket_style, HIGH_VISIBILITY):
            self.regions.store_sel(regions)

    def match_scope_brackets(self, bfr, sel, adj_dir):
        """
        See if scope should be searched, and then check
        endcaps to determine if valid scope bracket.
        """

        center = sel.a
        left = None
        right = None
        scope_count = 0
        before_center = center - 1
        bracket_count = 0
        partial_find = None
        max_size = self.view.size() - 1
        selected_scope = None
        bracket = None
        self.adjusted_center = center

        def is_scope(center, before_center, scope):
            match = False
            if before_center > 0:
                match = (
                    self.view.match_selector(center, scope) and
                    self.view.match_selector(before_center, scope)
                )
            if not match and self.bracket_out_adj:
                if adj_dir < 0:
                    if before_center > 0:
                        match = self.view.match_selector(before_center, scope)
                        if match:
                            self.adjusted_center = before_center
                else:
                    match = self.view.match_selector(center, scope)
                    if match:
                        self.adjusted_center += 1
            return match

        # Cannot be inside a bracket pair if cursor is at zero
        if center == 0:
            if not self.bracket_out_adj:
                return left, right, selected_scope, False

        # Identify if the cursor is in a scope with bracket definitions
        for s in self.scopes:
            scope = s["name"]
            extent = None
            exceed_limit = False
            if is_scope(center, before_center, scope):
                extent = self.view.extract_scope(self.adjusted_center)
                while extent is not None and not exceed_limit and extent.begin() != 0:
                    if self.view.match_selector(extent.begin() - 1, scope):
                        extent = extent.cover(self.view.extract_scope(extent.begin() - 1))
                        if extent.begin() < self.search_window[0] or extent.end() > self.search_window[1]:
                            extent = None
                            exceed_limit = True
                    else:
                        break
                while extent is not None and not exceed_limit and extent.end() != max_size:
                    if self.view.match_selector(extent.end(), scope):
                        extent = extent.cover(self.view.extract_scope(extent.end()))
                        if extent.begin() < self.search_window[0] or extent.end() > self.search_window[1]:
                            extent = None
                            exceed_limit = True
                    else:
                        break

            if extent is None:
                scope_count += 1
                continue

            # Search the bracket patterns of this scope
            # to determine if this scope matches the rules.
            bracket_count = 0
            scope_bfr = bfr[extent.begin():extent.end()]
            for b in s["brackets"]:
                m = b.open.search(scope_bfr)
                if m and m.group(1):
                    left = bh_search.ScopeEntry(extent.begin() + m.start(1), extent.begin() + m.end(1), scope_count, bracket_count)
                    if left is not None and not self.validate(left, 0, bfr, True):
                        left = None
                m = b.close.search(scope_bfr)
                if m and m.group(1):
                    right = bh_search.ScopeEntry(extent.begin() + m.start(1), extent.begin() + m.end(1), scope_count, bracket_count)
                    if right is not None and not self.validate(right, 1, bfr, True):
                        right = None
                if not self.compare(left, right, bfr, scope_bracket=True):
                    left, right = None, None
                # Track partial matches.  If a full match isn't found,
                # return the first partial match at the end.
                if partial_find is None and bool(left) != bool(right):
                    partial_find = (left, right)
                    left = None
                    right = None
                if left and right:
                    break
                bracket_count += 1
            if left and right:
                break
            scope_count += 1

        # Full match not found.  Return partial match (if any).
        if (left is None or right is None) and partial_find is not None:
            left, right = partial_find[0], partial_find[1]

        # Make sure cursor in highlighted sub group
        if (left and self.adjusted_center <= left.begin) or (right and self.adjusted_center >= right.end):
            left, right = None, None

        if left is not None:
            selected_scope = self.scopes[left.scope]["name"]
        elif right is not None:
            selected_scope = self.scopes[right.scope]["name"]

        if left is not None and right is not None:
            bracket = self.scopes[left.scope]["brackets"][left.type]
            if bracket.sub_search:
                self.sub_search_mode = True
                if self.sub_search(sel, (left.begin, right.end), bfr, scope):
                    return left, right, self.brackets[left.type], True
                elif bracket.sub_search_only:
                    left, right, bracket = None, None, None

        if self.adj_only:
            left, right = self.adjacent_check(left, right, center)

        left, right = self.post_match(left, right, center, bfr, scope_bracket=True)
        return left, right, bracket, False

    def match_brackets(self, bfr, window, sel, scope=None):
        """
        Regex bracket matching.
        """

        center = sel.a
        left = None
        right = None
        stack = []
        pattern = self.pattern if not self.sub_search_mode else self.sub_pattern
        bsearch = bh_search.BracketSearch(
            bfr, window, center,
            pattern, self.bracket_out_adj,
            self.is_illegal_scope, scope
        )
        if self.bracket_out_adj and not bsearch.touch_right and not self.recursive_guard:
            if self.find_scopes(bfr, sel, 1):
                return None, None, True
            self.sub_search_mode = False
        for o in bsearch.get_open(bh_search.BracketSearchSide.left):
            if not self.validate(o, 0, bfr):
                continue
            if len(stack) and bsearch.is_done(bh_search.BracketSearchType.closing):
                if self.compare(o, stack[-1], bfr):
                    stack.pop()
                    continue
            for c in bsearch.get_close(bh_search.BracketSearchSide.left):
                if not self.validate(c, 1, bfr):
                    continue
                if o.end <= c.begin:
                    stack.append(c)
                    continue
                elif len(stack):
                    bsearch.remember(bh_search.BracketSearchType.closing)
                    break

            if len(stack):
                b = stack.pop()
                if self.compare(o, b, bfr):
                    continue
            else:
                left = o
            break

        bsearch.reset_end_state()
        stack = []

        # Grab each closest closing right side bracket and attempt to match it.
        # If the closing bracket cannot be matched, select it.
        for c in bsearch.get_close(bh_search.BracketSearchSide.right):
            if not self.validate(c, 1, bfr):
                continue
            if len(stack) and bsearch.is_done(bh_search.BracketSearchType.opening):
                if self.compare(stack[-1], c, bfr):
                    stack.pop()
                    continue
            for o in bsearch.get_open(bh_search.BracketSearchSide.right):
                if not self.validate(o, 0, bfr):
                    continue
                if o.end <= c.begin:
                    stack.append(o)
                    continue
                else:
                    bsearch.remember(bh_search.BracketSearchType.opening)
                    break

            if len(stack):
                b = stack.pop()
                if self.compare(b, c, bfr):
                    continue
            else:
                if left is None or self.compare(left, c, bfr):
                    right = c
            break

        if self.adj_only:
            left, right = self.adjacent_check(left, right, center)

        left, right = self.post_match(left, right, center, bfr)
        return left, right, False

    def escaped(self, pt, ignore_string_escape, scope):
        """
        Check if sub bracket in string scope is escaped.
        """

        if not ignore_string_escape:
            return False
        if scope and scope.startswith("string"):
            return self.string_escaped(pt)
        return False

    def string_escaped(self, pt):
        """
        Check if bracket is follows escaping characters.
        Account for if in string or regex string scope.
        """

        escaped = False
        start = pt - 1
        first = False
        if self.view.settings().get("bracket_string_escape_mode", self.default_string_escape_mode) == "string":
            first = True
        while self.view.substr(start) == "\\":
            if first:
                first = False
            else:
                escaped = False if escaped else True
            start -= 1
        return escaped

    def is_illegal_scope(self, pt, bracket_id, scope=None):
        """
        Check if scope at pt X should be ignored.
        """

        bracket = self.brackets[bracket_id]
        if self.sub_search_mode and not bracket.find_in_sub_search:
            return True
        illegal_scope = False
        # Scope sent in, so we must be scanning whatever this scope is
        if scope is not None:
            if self.escaped(pt, bracket.ignore_string_escape, scope):
                illegal_scope = True
            return illegal_scope
        # for exception in bracket.scope_exclude_exceptions:
        elif len(bracket.scope_exclude_exceptions) and self.view.match_selector(pt, ", ".join(bracket.scope_exclude_exceptions)):
            pass
        elif len(bracket.scope_exclude) and self.view.match_selector(pt, ", ".join(bracket.scope_exclude)):
            illegal_scope = True
        return illegal_scope

    def adjacent_check(self, left, right, center):
        """
        Check if bracket pair are adjacent to cursor
        """

        if left and right:
            if left.end < center < right.begin:
                left, right = None, None
        elif (left and left.end < center) or (right and center < right.begin):
            left, right = None, None
        return left, right


class BhListenerCommand(sublime_plugin.EventListener):
    """
    Manage when to kick off bracket matching.
    Try and reduce redundant requests by letting the
    background thread ensure certain needed match occurs
    """

    def on_load(self, view):
        """
        Search brackets on view load.
        """

        if self.ignore_event(view):
            return
        BhEventMgr.type = BH_MATCH_TYPE_SELECTION
        sublime.set_timeout(bh_run, 0)

    def on_modified(self, view):
        """
        Update highlighted brackets when the text changes.
        """

        if self.ignore_event(view):
            return
        BhEventMgr.type = BH_MATCH_TYPE_EDIT
        BhEventMgr.modified = True
        BhEventMgr.time = time()

    def on_activated(self, view):
        """
        Highlight brackets when the view gains focus again.
        """

        if self.ignore_event(view):
            return
        BhEventMgr.type = BH_MATCH_TYPE_SELECTION
        sublime.set_timeout(bh_run, 0)

    def on_selection_modified(self, view):
        """
        Highlight brackets when the selections change.
        """

        if self.ignore_event(view):
            return
        if BhEventMgr.type != BH_MATCH_TYPE_EDIT:
            BhEventMgr.type = BH_MATCH_TYPE_SELECTION
        now = time()
        if now - BhEventMgr.time > BhEventMgr.wait_time:
            sublime.set_timeout(bh_run, 0)
        else:
            BhEventMgr.modified = True
            BhEventMgr.time = now

    def ignore_event(self, view):
        """
        Ignore request to highlight if the view is a widget,
        or if it is too soon to accept an event.
        """

        return (view.settings().get('is_widget') or BhEventMgr.ignore_all)


def bh_run():
    """
    Kick off matching of brackets
    """

    BhEventMgr.modified = False
    window = sublime.active_window()
    view = window.active_view() if window is not None else None
    BhEventMgr.ignore_all = True
    if bh_match is not None:
        bh_match(view, BhEventMgr.type == BH_MATCH_TYPE_EDIT)
    BhEventMgr.ignore_all = False
    BhEventMgr.time = time()


def bh_loop():
    """
    Start thread that will ensure highlighting happens after a barage of events
    Initial highlight is instant, but subsequent events in close succession will
    be ignored and then accounted for with one match by this thread
    """

    while not BhThreadMgr.restart:
        if BhEventMgr.modified is True and time() - BhEventMgr.time > BhEventMgr.wait_time:
            sublime.set_timeout(bh_run, 0)
        sleep(0.5)

    if BhThreadMgr.restart:
        BhThreadMgr.restart = False
        sublime.set_timeout(lambda: thread.start_new_thread(bh_loop, ()), 0)


def init_bh_match():
    """
    Initialize the match object
    """

    global bh_match
    bh_match = BhCore().match
    bh_logging.bh_debug("Match object loaded.")


def plugin_loaded():
    """
    Load up uniocode table, initialize settings and match object,
    and start event loop.  Restart event loop if already loaded.
    """

    init_bh_match()
    ure.set_cache_directory(join(sublime.packages_path(), "User"), "bh")

    global HIGH_VISIBILITY
    if sublime.load_settings("bh_core.sublime-settings").get('high_visibility_enabled_by_default', False):
        HIGH_VISIBILITY = True

    if 'running_bh_loop' not in globals():
        global running_bh_loop
        running_bh_loop = True
        thread.start_new_thread(bh_loop, ())
        bh_logging.bh_debug("Starting Thread")
    else:
        bh_logging.bh_debug("Restarting Thread")
        BhThreadMgr.restart = True
