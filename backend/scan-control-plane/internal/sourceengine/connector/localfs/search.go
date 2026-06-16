package localfs

import (
	"context"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"github.com/lazymind/scan_control_plane/internal/sourceengine/connector"
)

type searchItem struct {
	info       PathInfo
	parentPath string
}

func (c *LocalFSConnector) search(ctx context.Context, req connector.SearchRequest) (connector.RawObjectPage, error) {
	if err := ctx.Err(); err != nil {
		return connector.RawObjectPage{}, err
	}
	req.AgentID = c.resolveAgentID(req.AgentID)
	keyword := strings.TrimSpace(req.Keyword)
	if err := c.validateSearchRequest(req, keyword); err != nil {
		return connector.RawObjectPage{}, err
	}
	page, err := c.recursiveSearch(ctx, req, keyword)
	if err != nil {
		return connector.RawObjectPage{}, err
	}
	return c.virtualTargetPage(page), nil
}

func (c *LocalFSConnector) validateSearchRequest(req connector.SearchRequest, keyword string) error {
	if keyword == "" {
		return connector.NewError(connector.ErrorCodeInvalidArgument, "keyword is required")
	}
	if req.TargetType != "" && req.TargetType != TargetTypeLocalPath {
		return connector.NewError(connector.ErrorCodeInvalidTarget, "target_type is not supported")
	}
	if c.agent == nil {
		return connector.NewError(connector.ErrorCodeInvalidArgument, "local_fs agent client is not configured")
	}
	if req.AgentID == "" {
		return connector.NewError(connector.ErrorCodeInvalidArgument, "agent_id is required")
	}
	return validatePageSize(req.PageSize, c.Spec().MaxPageSize)
}

func (c *LocalFSConnector) recursiveSearch(ctx context.Context, req connector.SearchRequest, keyword string) (connector.RawObjectPage, error) {
	offset, err := parseCursor(req.Cursor)
	if err != nil {
		return connector.RawObjectPage{}, err
	}
	queue, err := c.searchRoots(ctx, req)
	if err != nil {
		return connector.RawObjectPage{}, err
	}
	items := make([]connector.RawObject, 0, req.PageSize)
	seenObjects := map[string]struct{}{}
	seenDirs := map[string]struct{}{}
	matchCount := 0
	for len(queue) > 0 {
		if err := ctx.Err(); err != nil {
			return connector.RawObjectPage{}, err
		}
		current := queue[0]
		queue = queue[1:]
		raw := c.rawObject(req.AgentID, current.info, current.parentPath)
		if _, ok := seenObjects[raw.ObjectKey]; ok {
			continue
		}
		seenObjects[raw.ObjectKey] = struct{}{}
		if localSearchNameMatches(current.info, keyword) {
			matchCount++
			if matchCount > offset {
				items = append(items, raw)
				if len(items) > req.PageSize {
					return connector.RawObjectPage{Items: items[:req.PageSize], HasMore: true, NextCursor: strconv.Itoa(offset + req.PageSize)}, nil
				}
			}
		}
		if !current.info.IsDir {
			continue
		}
		dirPath := canonicalPath(current.info)
		if _, ok := seenDirs[dirPath]; ok {
			continue
		}
		seenDirs[dirPath] = struct{}{}
		children, err := c.listSearchChildren(ctx, req, dirPath)
		if err != nil {
			return connector.RawObjectPage{}, err
		}
		queue = append(queue, children...)
	}
	return connector.RawObjectPage{Items: items, ListComplete: true}, nil
}

func (c *LocalFSConnector) searchRoots(ctx context.Context, req connector.SearchRequest) ([]searchItem, error) {
	if strings.TrimSpace(req.NodeRef) != "" || strings.TrimSpace(req.TargetRef) != "" {
		path, err := c.decodeNodeRef(req.TargetRef, req.NodeRef)
		if err != nil {
			return nil, err
		}
		info, err := c.agent.StatPath(ctx, StatPathRequest{AgentID: req.AgentID, Path: path})
		if err != nil {
			return nil, err
		}
		info, err = c.validateProbedPath(info)
		if err != nil {
			return nil, err
		}
		return []searchItem{{info: info, parentPath: filepath.Dir(canonicalPath(info))}}, nil
	}
	roots, err := c.initialRootInfos(ctx, connector.ListChildrenRequest{AgentID: req.AgentID, ProviderOptions: req.ProviderOptions})
	if err != nil {
		return nil, err
	}
	items := make([]searchItem, 0, len(roots))
	for _, root := range roots {
		info, err := c.validateProbedPath(root)
		if err != nil {
			continue
		}
		items = append(items, searchItem{info: info})
	}
	slices.SortFunc(items, func(a, b searchItem) int {
		aName := displayName(a.info.DisplayName, canonicalPath(a.info))
		bName := displayName(b.info.DisplayName, canonicalPath(b.info))
		if aName == bName {
			return strings.Compare(canonicalPath(a.info), canonicalPath(b.info))
		}
		return strings.Compare(aName, bName)
	})
	return items, nil
}

func (c *LocalFSConnector) listSearchChildren(ctx context.Context, req connector.SearchRequest, parentPath string) ([]searchItem, error) {
	var out []searchItem
	cursor := ""
	for {
		page, err := c.agent.ListDir(ctx, ListDirRequest{
			AgentID:      req.AgentID,
			Path:         parentPath,
			Cursor:       cursor,
			PageSize:     c.Spec().MaxPageSize,
			IncludeFiles: true,
		})
		if err != nil {
			return nil, err
		}
		for _, item := range page.Items {
			out = append(out, searchItem{info: item, parentPath: parentPath})
		}
		if !page.HasMore {
			return out, nil
		}
		if strings.TrimSpace(page.NextCursor) == "" {
			return nil, connector.NewError(connector.ErrorCodeTransient, "local_fs pagination cursor is empty")
		}
		cursor = page.NextCursor
	}
}

func localSearchNameMatches(info PathInfo, keyword string) bool {
	keyword = strings.ToLower(keyword)
	name := strings.ToLower(displayName(info.DisplayName, canonicalPath(info)))
	path := strings.ToLower(canonicalPath(info))
	return strings.Contains(name, keyword) || strings.Contains(path, keyword)
}
